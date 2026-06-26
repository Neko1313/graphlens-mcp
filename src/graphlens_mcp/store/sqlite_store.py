"""SQLite-backed graph store with WAL, single-writer, and cycle-safe CTEs."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Protocol

import aiosqlite
from graphlens import GraphLens, RelationKind
from graphlens.serialization import SCHEMA_VERSION
from graphlens.utils.serde import encode_metadata

logger = logging.getLogger(__name__)

_SCHEMA_SQL = Path(__file__).parent / "schema.sql"

# Bump this whenever schema.sql changes shape (new column/table/index). The graph DB
# is a regenerable cache, so a change drops and rebuilds it rather than migrating it
# (see ARCHITECTURE.md). The stored fingerprint also folds in graphlens' own model
# SCHEMA_VERSION, so a core model change invalidates the cache too.
LOCAL_SCHEMA_VERSION = 1

_ALL_TABLES = ("nodes_fts", "edges", "nodes", "deps", "files", "meta")

_GRAPH_STATUS_PRIORITY = {"skeleton": 0, "degraded": 1, "ok": 2}


def _schema_fingerprint() -> str:
    return f"{SCHEMA_VERSION}.{LOCAL_SCHEMA_VERSION}"


async def _apply_schema(conn: aiosqlite.Connection) -> None:
    # Use executescript rather than a naive split(";"): schema.sql contains a SQL
    # comment with a literal ';' inside it, which a hand-rolled splitter would cut
    # mid-statement and corrupt (it would drop every table after `edges`). SQLite's
    # own multi-statement parser handles comments and statement boundaries correctly.
    schema = _SCHEMA_SQL.read_text()
    await conn.executescript(schema)


def _worst_status(*statuses: str) -> str:
    return min(statuses, key=lambda s: _GRAPH_STATUS_PRIORITY.get(s, 0))


class SqliteStore:
    """Async SQLite graph store. Use :meth:`create` to instantiate.

    Concurrency model: a single aiosqlite connection serializes all operations, and
    every write is additionally guarded by ``_write_lock`` so multi-statement patches
    apply atomically. Reads therefore queue behind in-flight writes (we do not run a
    separate reader connection); WAL is enabled mainly for crash-safety and to keep
    the on-disk file consistent. For a single-user local server this serialized model
    is simpler and the write bursts (re-index of one changed file) are short.
    """

    def __init__(self, conn: aiosqlite.Connection, db_path: Path) -> None:
        self._conn = conn
        self._db_path = db_path
        self._write_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Construction / lifecycle
    # ------------------------------------------------------------------

    @classmethod
    async def create(cls, db_path: Path) -> SqliteStore:
        """Open (creating if needed) the store at *db_path* and apply the schema.

        If the stored schema fingerprint no longer matches the current one the cache
        is dropped and rebuilt from scratch (the graph is regenerable; we never
        migrate). Returns a ready-to-use store.
        """
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(db_path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA busy_timeout=5000")
        await _apply_schema(conn)
        await conn.commit()

        store = cls(conn, db_path)
        fingerprint = _schema_fingerprint()
        stored = await store._get_meta("schema_fingerprint")
        if stored is None:
            await store._set_meta("schema_fingerprint", fingerprint)
        elif stored != fingerprint:
            logger.warning(
                "Graph schema changed (%s -> %s); rebuilding the cache.", stored, fingerprint
            )
            await store._rebuild_schema()
            await store._set_meta("schema_fingerprint", fingerprint)

        return store

    async def _rebuild_schema(self) -> None:
        """Drop every table and re-apply schema.sql (handles column/shape changes)."""
        async with self._write_lock:
            for table in _ALL_TABLES:
                await self._conn.execute(f"DROP TABLE IF EXISTS {table}")
            await _apply_schema(self._conn)
            await self._conn.commit()

    async def close(self) -> None:
        """Close the underlying database connection."""
        await self._conn.close()

    # ------------------------------------------------------------------
    # Meta helpers
    # ------------------------------------------------------------------

    async def _get_meta(self, key: str) -> str | None:
        async with self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
            return row["value"] if row else None

    async def _set_meta(self, key: str, value: str) -> None:
        async with self._write_lock:
            await self._conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            await self._conn.commit()

    async def get_schema_fingerprint(self) -> str | None:
        """Return the schema fingerprint stored in the database, if any."""
        return await self._get_meta("schema_fingerprint")

    # ------------------------------------------------------------------
    # File freshness
    # ------------------------------------------------------------------

    async def get_file_info(self, path: str) -> dict[str, Any] | None:
        """Return the ``files`` row for *path*, or None if it is not indexed."""
        async with self._conn.execute(
            "SELECT path, hash, mtime, size, status, language FROM files WHERE path = ?",
            (path,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def update_file_mtime(self, path: str, mtime: float, size: int) -> None:
        """Refresh the recorded mtime/size for *path* without re-indexing it."""
        async with self._write_lock:
            await self._conn.execute(
                "UPDATE files SET mtime=?, size=? WHERE path=?",
                (mtime, size, path),
            )
            await self._conn.commit()

    async def get_worst_status_for_files(self, file_paths: list[str]) -> str:
        """Return the least-complete graph status across *file_paths*."""
        if not file_paths:
            return "ok"
        # placeholders is a count of '?' binds, never user data.
        placeholders = ",".join("?" * len(file_paths))
        async with self._conn.execute(
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
        """Apply the graph delta for one file atomically."""
        async with self._write_lock:
            async with self._conn.execute(
                "SELECT id, name, qualified_name FROM nodes WHERE file_path = ?",
                (file_path,),
            ) as cur:
                old_nodes = await cur.fetchall()

            # Remove old FTS entries
            for row in old_nodes:
                await self._conn.execute("DELETE FROM nodes_fts WHERE node_id = ?", (row["id"],))

            old_ids = {r["id"] for r in old_nodes}

            # Delete old edges owned by this file's nodes, but PRESERVE synthesized
            # cross-language edges: COMMUNICATES_WITH is produced by the full-index
            # link pass, not by single-file analysis, so re-inserting the file would
            # never regenerate it. Keeping it here stops it eroding on incremental
            # re-index (a full `reindex` still rebuilds it from scratch).
            if old_ids:
                # placeholders is a count of '?' binds, never user data.
                placeholders = ",".join("?" * len(old_ids))
                await self._conn.execute(
                    f"DELETE FROM edges WHERE source_id IN ({placeholders}) AND kind != ?",  # noqa: S608
                    [*old_ids, RelationKind.COMMUNICATES_WITH.value],
                )

            # Delete old nodes
            await self._conn.execute("DELETE FROM nodes WHERE file_path = ?", (file_path,))

            # Delete old deps for this file
            await self._conn.execute("DELETE FROM deps WHERE importer_path = ?", (file_path,))

            # Insert new nodes
            for node in graph.nodes.values():
                span_json = _encode_span(node.span) if node.span else None
                meta_json = json.dumps(encode_metadata(node.metadata)) if node.metadata else None
                await self._conn.execute(
                    "INSERT INTO nodes"
                    "(id, kind, qualified_name, name, file_path, span_json, metadata_json) "
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
            owned_ids = {n.id for n in graph.nodes.values() if n.file_path == file_path}
            for rel in graph.relations:
                if rel.source_id not in owned_ids:
                    continue
                meta_json = json.dumps(encode_metadata(rel.metadata)) if rel.metadata else None
                await self._conn.execute(
                    "INSERT OR IGNORE INTO edges(source_id, target_id, kind, metadata_json) "
                    "VALUES(?, ?, ?, ?)",
                    (rel.source_id, rel.target_id, rel.kind.value, meta_json),
                )
                # Track deps from IMPORTS edges
                if rel.kind == RelationKind.IMPORTS:
                    target = graph.nodes.get(rel.target_id)
                    if target and target.file_path:
                        await self._conn.execute(
                            "INSERT OR IGNORE INTO deps(importer_path, imported_path) VALUES(?, ?)",
                            (file_path, target.file_path),
                        )

            # Update FTS for new nodes in this file
            for node in graph.nodes.values():
                if node.file_path == file_path:
                    await self._conn.execute(
                        "INSERT INTO nodes_fts(name, qualified_name, node_id) VALUES(?, ?, ?)",
                        (node.name, node.qualified_name, node.id),
                    )

            # Update files table
            await self._conn.execute(
                "INSERT INTO files(path, hash, mtime, size, status, language) "
                "VALUES(?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(path) DO UPDATE SET "
                "  hash=excluded.hash, mtime=excluded.mtime, size=excluded.size,"
                "  status=excluded.status, language=excluded.language",
                (file_path, file_hash, mtime, size, status, language),
            )

            await self._conn.commit()

    async def apply_structural(self, graph: GraphLens) -> int:
        """Persist fileless structural nodes and the edges sourced from them.

        graphlens emits a hierarchy above files — ``project``/``module`` nodes (and
        cross-language ``boundary`` nodes) carry ``file_path=None``. The per-file
        :meth:`apply_patch` only inserts edges whose *source* belongs to a file, so
        the ``contains`` hierarchy edges (project/module → …) are otherwise dropped
        and the stored graph diverges from a full ``analyze()``. This pass runs after
        a full index (like cross-language linking), is idempotent, and is left
        untouched by incremental patches — those key on ``file_path`` / file-owned
        source ids, neither of which matches these fileless rows. Returns the number
        of structural edges inserted.
        """
        async with self._write_lock:
            fileless_ids: set[str] = set()
            for node in graph.nodes.values():
                if node.file_path is not None:
                    continue
                fileless_ids.add(node.id)
                span_json = _encode_span(node.span) if node.span else None
                meta_json = json.dumps(encode_metadata(node.metadata)) if node.metadata else None
                await self._conn.execute(
                    "INSERT INTO nodes"
                    "(id, kind, qualified_name, name, file_path, span_json, metadata_json) "
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
                await self._conn.execute("DELETE FROM nodes_fts WHERE node_id = ?", (node.id,))
                await self._conn.execute(
                    "INSERT INTO nodes_fts(name, qualified_name, node_id) VALUES(?, ?, ?)",
                    (node.name, node.qualified_name, node.id),
                )

            inserted = 0
            for rel in graph.relations:
                if rel.source_id not in fileless_ids:
                    continue
                meta_json = json.dumps(encode_metadata(rel.metadata)) if rel.metadata else None
                cur = await self._conn.execute(
                    "INSERT OR IGNORE INTO edges(source_id, target_id, kind, metadata_json) "
                    "VALUES(?, ?, ?, ?)",
                    (rel.source_id, rel.target_id, rel.kind.value, meta_json),
                )
                inserted += cur.rowcount
            await self._conn.commit()
            return inserted

    async def apply_cross_language_edges(self, relations: list[tuple[str, str, str]]) -> None:
        """Persist synthesized COMMUNICATES_WITH edges after merging graphs."""
        async with self._write_lock:
            for source_id, target_id, kind in relations:
                await self._conn.execute(
                    "INSERT OR IGNORE INTO edges(source_id, target_id, kind) VALUES(?, ?, ?)",
                    (source_id, target_id, kind),
                )
            await self._conn.commit()

    async def delete_file(self, file_path: str) -> bool:
        """Prune all graph state owned by *file_path* (e.g. a file deleted on disk).

        Removes the file's nodes, the edges sourced from them, its FTS rows, its
        dep entries and its row in ``files``. Cross-file edges that merely *target*
        this file's nodes are left untouched and resolve to nothing on read
        (dangling targets are filtered when querying). Returns True if the file
        was present in the store.
        """
        async with self._write_lock:
            async with self._conn.execute(
                "SELECT id FROM nodes WHERE file_path = ?", (file_path,)
            ) as cur:
                ids = [r["id"] for r in await cur.fetchall()]

            for node_id in ids:
                await self._conn.execute("DELETE FROM nodes_fts WHERE node_id = ?", (node_id,))
            if ids:
                # placeholders is a count of '?' binds, never user data.
                placeholders = ",".join("?" * len(ids))
                await self._conn.execute(
                    f"DELETE FROM edges WHERE source_id IN ({placeholders})",  # noqa: S608
                    ids,
                )
            await self._conn.execute("DELETE FROM nodes WHERE file_path = ?", (file_path,))
            await self._conn.execute("DELETE FROM deps WHERE importer_path = ?", (file_path,))
            cur = await self._conn.execute("DELETE FROM files WHERE path = ?", (file_path,))
            await self._conn.commit()
            return cur.rowcount > 0

    async def clear_all(self) -> None:
        """Wipe every table — used for a full rebuild and schema-version resets."""
        async with self._write_lock:
            for table in ("nodes_fts", "edges", "nodes", "deps", "files", "meta"):
                await self._conn.execute(f"DELETE FROM {table}")  # noqa: S608 — fixed table allowlist
            await self._conn.commit()

    # ------------------------------------------------------------------
    # Query API
    # ------------------------------------------------------------------

    async def search_symbols(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """Full-text search over symbol names."""
        try:
            sql = (
                "SELECT n.id, n.kind, n.qualified_name, n.name, n.file_path "
                "FROM nodes_fts f "
                "JOIN nodes n ON n.id = f.node_id "
                "WHERE nodes_fts MATCH ? "
                "LIMIT ?"
            )
            async with self._conn.execute(sql, (query, limit)) as cur:
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
            async with self._conn.execute(sql, (pattern, pattern, limit)) as cur:
                rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def get_node(self, node_id: str) -> dict[str, Any] | None:
        """Return the full node row for *node_id*, or None if absent."""
        async with self._conn.execute(
            "SELECT id, kind, qualified_name, name, file_path, span_json, metadata_json "
            "FROM nodes WHERE id = ?",
            (node_id,),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def get_nodes_in_file(self, file_path: str) -> list[dict[str, Any]]:
        """Return all node rows defined in *file_path*, ordered by kind/name."""
        async with self._conn.execute(
            "SELECT id, kind, qualified_name, name, span_json FROM nodes "
            "WHERE file_path = ? ORDER BY kind, name",
            (file_path,),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_callees(self, node_id: str, max_depth: int = 3) -> list[dict[str, Any]]:
        """Return nodes that node_id calls (outgoing CALLS), with cycle protection."""
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
        async with self._conn.execute(sql, {"start": node_id, "max_depth": max_depth}) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_callers(self, node_id: str, max_depth: int = 3) -> list[dict[str, Any]]:
        """Return nodes that call node_id (incoming CALLS), with cycle protection."""
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
        async with self._conn.execute(sql, {"start": node_id, "max_depth": max_depth}) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_neighbors(self, node_id: str, depth: int = 2) -> list[dict[str, Any]]:
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
        async with self._conn.execute(sql, {"start": node_id, "depth": depth}) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def find_references(self, node_id: str) -> list[dict[str, Any]]:
        """Return nodes that reference node_id (incoming REFERENCES edges)."""
        sql = (
            "SELECT DISTINCT n.id, n.kind, n.qualified_name, n.name, n.file_path "
            "FROM edges e "
            "JOIN nodes n ON n.id = e.source_id "
            "WHERE e.target_id = ? AND e.kind = 'references'"
        )
        async with self._conn.execute(sql, (node_id,)) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_cross_language_calls(self, node_id: str) -> list[dict[str, Any]]:
        """Return nodes connected via COMMUNICATES_WITH or through a shared BOUNDARY."""
        # Direct COMMUNICATES_WITH edges
        sql_direct = (
            "SELECT DISTINCT n.id, n.kind, n.qualified_name, n.name, n.file_path, "
            "  'communicates_with' AS relation "
            "FROM edges e "
            "JOIN nodes n ON n.id = e.target_id "
            "WHERE e.source_id = ? AND e.kind = 'communicates_with' "
            "UNION "
            "SELECT DISTINCT n.id, n.kind, n.qualified_name, n.name, n.file_path, "
            "  'communicates_with' AS relation "
            "FROM edges e "
            "JOIN nodes n ON n.id = e.source_id "
            "WHERE e.target_id = ? AND e.kind = 'communicates_with'"
        )
        async with self._conn.execute(sql_direct, (node_id, node_id)) as cur:
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
        async with self._conn.execute(sql_boundary, {"nid": node_id}) as cur:
            rows = await cur.fetchall()

        seen = {r["id"] for r in results}
        for row in rows:
            if row["id"] not in seen:
                seen.add(row["id"])
                results.append(
                    {k: row[k] for k in ("id", "kind", "qualified_name", "name", "file_path")}
                )

        return results

    async def node_count(self) -> int:
        """Return the total number of nodes in the graph."""
        async with self._conn.execute("SELECT COUNT(*) FROM nodes") as cur:
            row = await cur.fetchone()
        return row[0] if row else 0

    async def edge_count(self) -> int:
        """Return the total number of edges in the graph."""
        async with self._conn.execute("SELECT COUNT(*) FROM edges") as cur:
            row = await cur.fetchone()
        return row[0] if row else 0

    async def file_count(self) -> int:
        """Return the number of indexed files."""
        async with self._conn.execute("SELECT COUNT(*) FROM files") as cur:
            row = await cur.fetchone()
        return row[0] if row else 0

    async def list_files(self) -> list[dict[str, Any]]:
        """Return path/status/language for every indexed file, ordered by path."""
        async with self._conn.execute(
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
    return json.dumps([span.start_line, span.start_col, span.end_line, span.end_col])
