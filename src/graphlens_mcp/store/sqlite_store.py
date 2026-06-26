"""SQLite-backed graph store with WAL, single-writer, and cycle-safe CTEs."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import aiosqlite
from graphlens import GraphLens, RelationKind
from graphlens.serialization import SCHEMA_VERSION
from graphlens.utils.serde import encode_metadata

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)

_SCHEMA_SQL = Path(__file__).parent / "schema.sql"

# Bump this whenever schema.sql changes shape (new column/table/index).
# The graph DB is a regenerable cache, so a change drops and rebuilds it
# rather than migrating it (see ARCHITECTURE.md). The stored fingerprint
# also folds in graphlens' own model SCHEMA_VERSION, so a core model
# change invalidates the cache too.
LOCAL_SCHEMA_VERSION = 2

_ALL_TABLES = ("nodes_fts", "edges", "nodes", "deps", "files", "meta")

_GRAPH_STATUS_PRIORITY = {"skeleton": 0, "degraded": 1, "ok": 2}


def _schema_fingerprint() -> str:
    return f"{SCHEMA_VERSION}.{LOCAL_SCHEMA_VERSION}"


async def _apply_schema(conn: aiosqlite.Connection) -> None:
    # Use executescript rather than a naive split(";"): schema.sql
    # contains a SQL comment with a literal ';' inside it, which a
    # hand-rolled splitter would cut mid-statement and corrupt (it would
    # drop every table after `edges`). SQLite's own multi-statement
    # parser handles comments and statement boundaries correctly.
    schema = _SCHEMA_SQL.read_text()
    await conn.executescript(schema)


def _worst_status(*statuses: str) -> str:
    return min(statuses, key=lambda s: _GRAPH_STATUS_PRIORITY.get(s, 0))


def worst_status(*statuses: str) -> str:
    """
    Return the least-complete of *statuses* (skeleton < degraded < ok).

    Public helper so the tool layer can fold a query's own freshness
    status together with the stored status of every node it returns.
    """
    return _worst_status(*statuses)


class SqliteStore:
    """
    Async SQLite graph store. Use :meth:`create` to instantiate.

    Concurrency model: a dedicated writer connection serializes all
    writes behind ``_write_lock`` so multi-statement patches apply
    atomically, while a separate read-only connection serves queries
    without queuing behind an in-flight write. WAL lets the reader see
    the last committed snapshot while a write is in progress, so a
    `get_callers` no longer blocks on a concurrent re-index. Every write
    is wrapped in :meth:`_writing`, which commits on success and rolls
    back on error so a failed multi-statement patch can never leave a
    partial transaction for the next writer.
    """

    def __init__(
        self,
        conn: aiosqlite.Connection,
        read_conn: aiosqlite.Connection,
        db_path: Path,
    ) -> None:
        """Wrap an open writer/reader connection pair for *db_path*."""
        self._conn = conn
        self._read_conn = read_conn
        self._db_path = db_path
        self._write_lock = asyncio.Lock()
        self._closed = False

    @contextlib.asynccontextmanager
    async def _writing(self) -> AsyncIterator[None]:
        """
        Run a write under the single-writer lock, committing or rolling back.

        Acquires ``_write_lock`` (so multi-statement patches are atomic
        against each other), commits on clean exit, and rolls back on any
        exception before re-raising — otherwise a half-applied patch
        would sit in the connection's pending transaction and be
        committed by the next writer.
        """
        async with self._write_lock:
            try:
                yield
                await self._conn.commit()
            except BaseException:
                await self._conn.rollback()
                raise

    # ------------------------------------------------------------------
    # Construction / lifecycle
    # ------------------------------------------------------------------

    @classmethod
    async def create(cls, db_path: Path) -> SqliteStore:
        """
        Open (creating if needed) the store at *db_path* and apply the schema.

        If the stored schema fingerprint no longer matches the current
        one the cache is dropped and rebuilt from scratch (the graph is
        regenerable; we never migrate). Returns a ready-to-use store.
        """
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(db_path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA busy_timeout=5000")
        await _apply_schema(conn)
        await conn.commit()

        # Dedicated read-only connection: queries read the last committed
        # WAL snapshot without queuing behind the writer's lock. Opened
        # after the schema exists.
        read_conn = await aiosqlite.connect(db_path)
        read_conn.row_factory = aiosqlite.Row
        await read_conn.execute("PRAGMA busy_timeout=5000")
        await read_conn.execute("PRAGMA query_only=ON")

        store = cls(conn, read_conn, db_path)
        fingerprint = _schema_fingerprint()
        stored = await store._get_meta("schema_fingerprint")
        if stored is None:
            await store._set_meta("schema_fingerprint", fingerprint)
        elif stored != fingerprint:
            logger.warning(
                "Graph schema changed (%s -> %s); rebuilding the cache.",
                stored,
                fingerprint,
            )
            await store._rebuild_schema()
            await store._set_meta("schema_fingerprint", fingerprint)

        return store

    async def _rebuild_schema(self) -> None:
        """Drop every table and re-apply schema.sql (column/shape changes)."""
        async with self._writing():
            for table in _ALL_TABLES:
                await self._conn.execute(f"DROP TABLE IF EXISTS {table}")
            await _apply_schema(self._conn)

    async def close(self) -> None:
        """Close the writer and reader database connections (idempotent)."""
        if self._closed:
            return
        self._closed = True
        await self._read_conn.close()
        await self._conn.close()

    # ------------------------------------------------------------------
    # Meta helpers
    # ------------------------------------------------------------------

    async def _get_meta(self, key: str) -> str | None:
        async with self._read_conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
            return row["value"] if row else None

    async def _set_meta(self, key: str, value: str) -> None:
        async with self._writing():
            await self._conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    async def get_schema_fingerprint(self) -> str | None:
        """Return the schema fingerprint stored in the database, if any."""
        return await self._get_meta("schema_fingerprint")

    # ------------------------------------------------------------------
    # File freshness
    # ------------------------------------------------------------------

    async def get_file_info(self, path: str) -> dict[str, Any] | None:
        """Return the ``files`` row for *path*, or None if not indexed."""
        async with self._read_conn.execute(
            "SELECT path, hash, mtime, size, status, language "
            "FROM files WHERE path = ?",
            (path,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_imported_paths(self, importer_path: str) -> list[str]:
        """Return the paths *importer_path* imports (recorded deps)."""
        async with self._read_conn.execute(
            "SELECT imported_path FROM deps WHERE importer_path = ?",
            (importer_path,),
        ) as cur:
            rows = await cur.fetchall()
        return [r["imported_path"] for r in rows]

    async def get_imported_files(self, importer_path: str) -> list[str]:
        """
        Return the set of files *importer_path* depends on (from graph).

        An ``IMPORTS`` edge often targets a fileless MODULE node (e.g.
        ``pkg.a``) rather than a file, so the ``deps`` table alone misses
        file-level dependencies. This resolves both: edges that point
        straight at a file, and module targets mapped to the files of the
        symbols that live under that module's qualified name. Works on
        incrementally-indexed graphs too, since it reads persisted
        nodes/edges directly.
        """
        sql = """
        SELECT DISTINCT tn.file_path AS dep
        FROM edges e
        JOIN nodes sn ON sn.id = e.source_id AND sn.file_path = :imp
        JOIN nodes tn ON tn.id = e.target_id AND tn.file_path IS NOT NULL
        WHERE e.kind = 'imports'
        UNION
        SELECT DISTINCT m.file_path AS dep
        FROM edges e
        JOIN nodes sn ON sn.id = e.source_id AND sn.file_path = :imp
        JOIN nodes tmod ON tmod.id = e.target_id AND tmod.file_path IS NULL
        JOIN nodes m ON m.file_path IS NOT NULL
          AND (m.qualified_name = tmod.qualified_name
               OR m.qualified_name LIKE tmod.qualified_name || '.%')
        WHERE e.kind = 'imports'
        """
        async with self._read_conn.execute(sql, {"imp": importer_path}) as cur:
            rows = await cur.fetchall()
        return [
            r["dep"] for r in rows if r["dep"] and r["dep"] != importer_path
        ]

    async def update_file_mtime(
        self, path: str, mtime: float, size: int
    ) -> None:
        """Refresh the recorded mtime/size for *path* without reindexing."""
        async with self._writing():
            await self._conn.execute(
                "UPDATE files SET mtime=?, size=? WHERE path=?",
                (mtime, size, path),
            )

    async def get_worst_status_for_files(self, file_paths: list[str]) -> str:
        """Return the least-complete graph status across *file_paths*."""
        if not file_paths:
            return "ok"
        # placeholders is a count of '?' binds, never user data.
        placeholders = ",".join("?" * len(file_paths))
        async with self._read_conn.execute(
            f"SELECT status FROM files WHERE path IN ({placeholders})",  # noqa: S608
            file_paths,
        ) as cur:
            rows = await cur.fetchall()
        statuses = [r["status"] for r in rows] if rows else ["skeleton"]
        return _worst_status(*statuses)

    # ------------------------------------------------------------------
    # Patch application (single-writer)
    # ------------------------------------------------------------------

    async def apply_patch(
        self,
        graph: GraphLens,
        file_path: str,
        file_hash: str,
        mtime: float,
        size: int,
        status: str,
        language: str,
    ) -> None:
        """
        Apply the graph delta for one file atomically.

        Only nodes the analysis attributes to *file_path* are persisted.
        A single-file ``analyze`` (and ``subgraph_for_file``) also
        surfaces foreign edge-target nodes from other files; inserting
        those here would let one file's patch overwrite a symbol owned by
        another file (upsert keys on the stable node id). Each file owns
        and writes only its own nodes — foreign targets are inserted by
        their own file's patch, and unresolved targets are filtered at
        read time (the dangling-edge model).
        """
        owned = [n for n in graph.nodes.values() if n.file_path == file_path]
        owned_ids = {n.id for n in owned}
        async with self._writing():
            async with self._conn.execute(
                "SELECT id FROM nodes WHERE file_path = ?",
                (file_path,),
            ) as cur:
                old_ids = {r["id"] for r in await cur.fetchall()}

            # Remove old FTS entries
            for old_id in old_ids:
                await self._conn.execute(
                    "DELETE FROM nodes_fts WHERE node_id = ?", (old_id,)
                )

            # Delete old edges owned by this file's nodes, but PRESERVE
            # synthesized cross-language edges: COMMUNICATES_WITH is
            # produced by the full-index link pass, not by single-file
            # analysis, so re-inserting the file would never regenerate
            # it. Keeping it here stops it eroding on incremental re-index
            # (a full `reindex` still rebuilds it from scratch).
            if old_ids:
                # placeholders is a count of '?' binds, never user data.
                placeholders = ",".join("?" * len(old_ids))
                await self._conn.execute(
                    "DELETE FROM edges "  # noqa: S608
                    f"WHERE source_id IN ({placeholders}) "
                    "AND kind != ?",
                    [*old_ids, RelationKind.COMMUNICATES_WITH.value],
                )

            # Delete old nodes
            await self._conn.execute(
                "DELETE FROM nodes WHERE file_path = ?", (file_path,)
            )

            # Delete old deps for this file
            await self._conn.execute(
                "DELETE FROM deps WHERE importer_path = ?", (file_path,)
            )

            # Insert this file's nodes (foreign targets owned elsewhere)
            for node in owned:
                span_json = _encode_span(node.span) if node.span else None
                meta_json = (
                    json.dumps(encode_metadata(node.metadata))
                    if node.metadata
                    else None
                )
                await self._conn.execute(
                    "INSERT INTO nodes"
                    "(id, kind, qualified_name, name, file_path, "
                    "span_json, metadata_json) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET "
                    "  kind=excluded.kind,"
                    "  qualified_name=excluded.qualified_name,"
                    "  name=excluded.name,"
                    "  file_path=excluded.file_path,"
                    "  span_json=excluded.span_json,"
                    "  metadata_json=excluded.metadata_json",
                    (
                        node.id,
                        node.kind.value,
                        node.qualified_name,
                        node.name,
                        node.file_path,
                        span_json,
                        meta_json,
                    ),
                )

            # Insert new edges (from nodes owned by this file)
            for rel in graph.relations:
                if rel.source_id not in owned_ids:
                    continue
                meta_json = (
                    json.dumps(encode_metadata(rel.metadata))
                    if rel.metadata
                    else None
                )
                await self._conn.execute(
                    "INSERT OR IGNORE INTO edges"
                    "(source_id, target_id, kind, metadata_json) "
                    "VALUES(?, ?, ?, ?)",
                    (rel.source_id, rel.target_id, rel.kind.value, meta_json),
                )
                # Track deps from IMPORTS edges
                if rel.kind == RelationKind.IMPORTS:
                    target = graph.nodes.get(rel.target_id)
                    if target and target.file_path:
                        await self._conn.execute(
                            "INSERT OR IGNORE INTO deps"
                            "(importer_path, imported_path) VALUES(?, ?)",
                            (file_path, target.file_path),
                        )

            # Update FTS for this file's nodes
            for node in owned:
                await self._conn.execute(
                    "INSERT INTO nodes_fts"
                    "(name, qualified_name, node_id) VALUES(?, ?, ?)",
                    (node.name, node.qualified_name, node.id),
                )

            # Update files table
            await self._conn.execute(
                "INSERT INTO files(path, hash, mtime, size, status, language) "
                "VALUES(?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(path) DO UPDATE SET "
                "  hash=excluded.hash, mtime=excluded.mtime, "
                "size=excluded.size,"
                "  status=excluded.status, language=excluded.language",
                (file_path, file_hash, mtime, size, status, language),
            )

    async def apply_structural(self, graph: GraphLens) -> int:
        """
        Persist fileless structural nodes and the edges sourced from them.

        graphlens emits a hierarchy above files — ``project``/``module``
        nodes (and cross-language ``boundary`` nodes) carry
        ``file_path=None``. The per-file :meth:`apply_patch` only inserts
        edges whose *source* belongs to a file, so the ``contains``
        hierarchy edges (project/module → …) are otherwise dropped and
        the stored graph diverges from a full ``analyze()``. This pass
        runs after a full index (like cross-language linking), is
        idempotent, and is left untouched by incremental patches — those
        key on ``file_path`` / file-owned source ids, neither of which
        matches these fileless rows. Returns the number of structural
        edges inserted.
        """
        async with self._writing():
            fileless_ids: set[str] = set()
            for node in graph.nodes.values():
                if node.file_path is not None:
                    continue
                fileless_ids.add(node.id)
                span_json = _encode_span(node.span) if node.span else None
                meta_json = (
                    json.dumps(encode_metadata(node.metadata))
                    if node.metadata
                    else None
                )
                await self._conn.execute(
                    "INSERT INTO nodes"
                    "(id, kind, qualified_name, name, file_path, "
                    "span_json, metadata_json) "
                    "VALUES(?, ?, ?, ?, NULL, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET "
                    "  kind=excluded.kind,"
                    "  qualified_name=excluded.qualified_name,"
                    "  name=excluded.name,"
                    "  span_json=excluded.span_json,"
                    "  metadata_json=excluded.metadata_json",
                    (
                        node.id,
                        node.kind.value,
                        node.qualified_name,
                        node.name,
                        span_json,
                        meta_json,
                    ),
                )
                # Keep structural nodes searchable; dedupe to stay idempotent.
                await self._conn.execute(
                    "DELETE FROM nodes_fts WHERE node_id = ?", (node.id,)
                )
                await self._conn.execute(
                    "INSERT INTO nodes_fts"
                    "(name, qualified_name, node_id) VALUES(?, ?, ?)",
                    (node.name, node.qualified_name, node.id),
                )

            inserted = 0
            for rel in graph.relations:
                if rel.source_id not in fileless_ids:
                    continue
                meta_json = (
                    json.dumps(encode_metadata(rel.metadata))
                    if rel.metadata
                    else None
                )
                cur = await self._conn.execute(
                    "INSERT OR IGNORE INTO edges"
                    "(source_id, target_id, kind, metadata_json) "
                    "VALUES(?, ?, ?, ?)",
                    (rel.source_id, rel.target_id, rel.kind.value, meta_json),
                )
                inserted += cur.rowcount
            return inserted

    async def apply_cross_language_edges(
        self, relations: list[tuple[str, str, str]]
    ) -> None:
        """Persist synthesized COMMUNICATES_WITH edges after merging graphs."""
        async with self._writing():
            for source_id, target_id, kind in relations:
                await self._conn.execute(
                    "INSERT OR IGNORE INTO edges"
                    "(source_id, target_id, kind) VALUES(?, ?, ?)",
                    (source_id, target_id, kind),
                )

    async def delete_file(self, file_path: str) -> bool:
        """
        Prune all graph state owned by *file_path* (e.g. deleted file).

        Removes the file's nodes, the edges sourced from them, its FTS
        rows, its dep entries and its row in ``files``. Cross-file edges
        that merely *target* this file's nodes are left untouched and
        resolve to nothing on read (dangling targets are filtered when
        querying). Returns True if the file was present in the store.
        """
        async with self._writing():
            async with self._conn.execute(
                "SELECT id FROM nodes WHERE file_path = ?", (file_path,)
            ) as cur:
                ids = [r["id"] for r in await cur.fetchall()]

            for node_id in ids:
                await self._conn.execute(
                    "DELETE FROM nodes_fts WHERE node_id = ?", (node_id,)
                )
            if ids:
                # placeholders is a count of '?' binds, never user data.
                placeholders = ",".join("?" * len(ids))
                await self._conn.execute(
                    f"DELETE FROM edges WHERE source_id IN ({placeholders})",  # noqa: S608
                    ids,
                )
            await self._conn.execute(
                "DELETE FROM nodes WHERE file_path = ?", (file_path,)
            )
            await self._conn.execute(
                "DELETE FROM deps WHERE importer_path = ?", (file_path,)
            )
            cur = await self._conn.execute(
                "DELETE FROM files WHERE path = ?", (file_path,)
            )
            return cur.rowcount > 0

    async def clear_all(self) -> None:
        """Wipe every table — used for a rebuild and schema-version resets."""
        async with self._writing():
            for table in (
                "nodes_fts",
                "edges",
                "nodes",
                "deps",
                "files",
                "meta",
            ):
                await self._conn.execute(f"DELETE FROM {table}")  # noqa: S608 — fixed table allowlist

    # ------------------------------------------------------------------
    # Query API
    # ------------------------------------------------------------------

    async def search_symbols(
        self, query: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Full-text search over symbol names."""
        try:
            sql = (
                "SELECT n.id, n.kind, n.qualified_name, n.name, n.file_path "
                "FROM nodes_fts f "
                "JOIN nodes n ON n.id = f.node_id "
                "WHERE nodes_fts MATCH ? "
                "LIMIT ?"
            )
            async with self._read_conn.execute(sql, (query, limit)) as cur:
                rows = await cur.fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.warning("FTS search failed for %r: %s", query, exc)
            # Fallback: LIKE search
            pattern = f"%{query}%"
            sql = (
                "SELECT id, kind, qualified_name, name, file_path FROM nodes "
                "WHERE name LIKE ? OR qualified_name LIKE ? LIMIT ?"
            )
            async with self._read_conn.execute(
                sql, (pattern, pattern, limit)
            ) as cur:
                rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def get_node(self, node_id: str) -> dict[str, Any] | None:
        """Return the full node row for *node_id*, or None if absent."""
        async with self._read_conn.execute(
            "SELECT id, kind, qualified_name, name, file_path, "
            "span_json, metadata_json "
            "FROM nodes WHERE id = ?",
            (node_id,),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def get_nodes_in_file(self, file_path: str) -> list[dict[str, Any]]:
        """Return node rows defined in *file_path*, ordered by kind/name."""
        async with self._read_conn.execute(
            "SELECT id, kind, qualified_name, name, span_json FROM nodes "
            "WHERE file_path = ? ORDER BY kind, name",
            (file_path,),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_callees(
        self, node_id: str, max_depth: int = 3
    ) -> list[dict[str, Any]]:
        """Return nodes node_id calls (outgoing CALLS), cycle-protected."""
        sql = """
        WITH RECURSIVE walk(id, depth, path) AS (
          SELECT :start, 0, ',' || :start || ','
          UNION ALL
          SELECT e.target_id, w.depth + 1, w.path || e.target_id || ','
          FROM edges e
          JOIN walk w ON e.source_id = w.id
          WHERE e.kind = 'calls'
            AND w.depth < :max_depth
            AND instr(w.path, ',' || e.target_id || ',') = 0
        )
        SELECT DISTINCT n.id, n.kind, n.qualified_name, n.name, n.file_path
        FROM walk
        JOIN nodes n ON n.id = walk.id
        WHERE walk.id != :start
        """
        async with self._read_conn.execute(
            sql, {"start": node_id, "max_depth": max_depth}
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_callers(
        self, node_id: str, max_depth: int = 3
    ) -> list[dict[str, Any]]:
        """Return nodes that call node_id (incoming CALLS), cycle-safe."""
        sql = """
        WITH RECURSIVE walk(id, depth, path) AS (
          SELECT :start, 0, ',' || :start || ','
          UNION ALL
          SELECT e.source_id, w.depth + 1, w.path || e.source_id || ','
          FROM edges e
          JOIN walk w ON e.target_id = w.id
          WHERE e.kind = 'calls'
            AND w.depth < :max_depth
            AND instr(w.path, ',' || e.source_id || ',') = 0
        )
        SELECT DISTINCT n.id, n.kind, n.qualified_name, n.name, n.file_path
        FROM walk
        JOIN nodes n ON n.id = walk.id
        WHERE walk.id != :start
        """
        async with self._read_conn.execute(
            sql, {"start": node_id, "max_depth": max_depth}
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_neighbors(
        self, node_id: str, depth: int = 2
    ) -> list[dict[str, Any]]:
        """Return nodes within *depth* hops in any direction."""
        sql = """
        WITH RECURSIVE walk(id, depth, path) AS (
          SELECT :start, 0, ',' || :start || ','
          UNION ALL
          SELECT nbr, w.depth + 1, w.path || nbr || ','
          FROM (
            SELECT e.target_id AS nbr, e.source_id AS via FROM edges e
            UNION ALL
            SELECT e.source_id AS nbr, e.target_id AS via FROM edges e
          ) pairs
          JOIN walk w ON pairs.via = w.id
          WHERE w.depth < :depth
            AND instr(w.path, ',' || pairs.nbr || ',') = 0
        )
        SELECT DISTINCT n.id, n.kind, n.qualified_name, n.name, n.file_path
        FROM walk
        JOIN nodes n ON n.id = walk.id
        WHERE walk.id != :start
        """
        async with self._read_conn.execute(
            sql, {"start": node_id, "depth": depth}
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def find_references(self, node_id: str) -> list[dict[str, Any]]:
        """Return nodes that reference node_id (incoming REFERENCES edges)."""
        sql = (
            "SELECT DISTINCT n.id, n.kind, n.qualified_name, n.name, "
            "n.file_path "
            "FROM edges e "
            "JOIN nodes n ON n.id = e.source_id "
            "WHERE e.target_id = ? AND e.kind = 'references'"
        )
        async with self._read_conn.execute(sql, (node_id,)) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_cross_language_calls(
        self, node_id: str
    ) -> list[dict[str, Any]]:
        """Return nodes linked via COMMUNICATES_WITH or a shared BOUNDARY."""
        # Direct COMMUNICATES_WITH edges
        sql_direct = (
            "SELECT DISTINCT n.id, n.kind, n.qualified_name, n.name, "
            "n.file_path, "
            "  'communicates_with' AS relation "
            "FROM edges e "
            "JOIN nodes n ON n.id = e.target_id "
            "WHERE e.source_id = ? AND e.kind = 'communicates_with' "
            "UNION "
            "SELECT DISTINCT n.id, n.kind, n.qualified_name, n.name, "
            "n.file_path, "
            "  'communicates_with' AS relation "
            "FROM edges e "
            "JOIN nodes n ON n.id = e.source_id "
            "WHERE e.target_id = ? AND e.kind = 'communicates_with'"
        )
        async with self._read_conn.execute(
            sql_direct, (node_id, node_id)
        ) as cur:
            rows = await cur.fetchall()
        results = [dict(r) for r in rows]

        # Via BOUNDARY nodes: find BOUNDARY nodes this node connects to,
        # then find other nodes connecting to the same boundary
        sql_boundary = """
        SELECT DISTINCT n.id, n.kind, n.qualified_name, n.name, n.file_path,
          b.id AS via_boundary
        FROM edges e1
        JOIN nodes b ON b.id = e1.target_id AND b.kind = 'boundary'
        JOIN edges e2 ON e2.target_id = b.id AND e2.source_id != :nid
        JOIN nodes n ON n.id = e2.source_id
        WHERE e1.source_id = :nid AND e1.kind IN ('exposes', 'consumes')
          AND e2.kind IN ('exposes', 'consumes')
        """
        async with self._read_conn.execute(
            sql_boundary, {"nid": node_id}
        ) as cur:
            rows = await cur.fetchall()

        seen = {r["id"] for r in results}
        for row in rows:
            if row["id"] not in seen:
                seen.add(row["id"])
                results.append(
                    {
                        k: row[k]
                        for k in (
                            "id",
                            "kind",
                            "qualified_name",
                            "name",
                            "file_path",
                        )
                    }
                )

        return results

    async def node_count(self) -> int:
        """Return the total number of nodes in the graph."""
        async with self._read_conn.execute(
            "SELECT COUNT(*) FROM nodes"
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else 0

    async def edge_count(self) -> int:
        """Return the total number of edges in the graph."""
        async with self._read_conn.execute(
            "SELECT COUNT(*) FROM edges"
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else 0

    async def file_count(self) -> int:
        """Return the number of indexed files."""
        async with self._read_conn.execute(
            "SELECT COUNT(*) FROM files"
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else 0

    async def list_files(self) -> list[dict[str, Any]]:
        """Return path/status/language per indexed file, ordered by path."""
        async with self._read_conn.execute(
            "SELECT path, status, language FROM files ORDER BY path"
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


class _Span(Protocol):
    start_line: int
    start_col: int
    end_line: int
    end_col: int


def _encode_span(span: _Span) -> str:
    return json.dumps(
        [span.start_line, span.start_col, span.end_line, span.end_col]
    )
