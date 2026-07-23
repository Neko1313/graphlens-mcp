import functools
import hashlib
import json
import re
from collections.abc import Iterable, Iterator
from pathlib import Path

from graphlens import Node, Relation

from shared.common.db.graph import GraphExecutor, GraphStore

__all__ = [
    "clear_project",
    "clear_relations",
    "content_hash",
    "delete_nodes",
    "ensure_code_schema",
    "gid",
    "graph_head",
    "norm_path",
    "persist_nodes",
    "persist_relations",
    "reset_source_cache",
    "set_graph_head",
    "stored_node_hashes",
    "vector_ids_filter",
    "vector_project_filter",
]

_BATCH = 500

# project ids are minted by compute_project_id with this charset, so inlining
# one into a Milvus filter expression is injection-safe — asserted at build.
_PID_RE = re.compile(r"^[A-Za-z0-9_]+$")
# gid = "{project_id}::{graphlens node id}" — node ids are hex digests, so the
# combined charset is bounded; asserted before inlining into an `id in [...]`.
_GID_RE = re.compile(r"^[A-Za-z0-9_:.\-]+$")


def gid(project_id: str, node_id: str) -> str:
    """Namespace a graphlens node id by project so ids never collide across
    projects sharing the one code-graph database (and the one vector
    collection).
    """
    return f"{project_id}::{node_id}"


@functools.lru_cache(maxsize=2048)
def _cached_lines(key: tuple[str, int, int]) -> tuple[str, ...]:
    # key = (path, size, mtime_ns): one file is read once per index run and
    # re-read only after it changes on disk.
    try:
        text = Path(key[0]).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ()
    return tuple(text.splitlines())


def reset_source_cache() -> None:
    """Drop the cached file lines — call once at the start of an index run.

    The cache dedups reads *within* a run (files are static there); clearing
    it between runs stops a stale entry surviving a same-size, same-mtime edit
    on a coarse-granularity filesystem (FAT/NFS) or an mtime-preserving
    restore.
    """
    _cached_lines.cache_clear()


def _span_text(root: Path, node: Node) -> str:
    """The node's own source lines — so a body edit is a content change."""
    if node.file_path is None or node.span is None:
        return ""
    path = Path(node.file_path)
    if not path.is_absolute():
        path = root / node.file_path
    try:
        info = path.stat()
    except OSError:
        return ""
    lines = _cached_lines((str(path), info.st_size, info.st_mtime_ns))
    start = max(node.span.start_line - 1, 0)
    end = min(node.span.end_line, len(lines))
    return "\n".join(lines[start:end])


def content_hash(root: Path, node: Node) -> str:
    """A stable digest of a node's identity-relevant content, body included.

    The single source of truth for "did this node change": incremental indexing
    skips re-embedding/re-writing a node whose hash is unchanged, and the
    temporal log uses it for its create/update decision — so both call THIS.
    The node's source span is folded in, so a body-only edit (which shifts the
    embedding but no structural field) still counts as a change.
    """
    payload = "\x00".join(
        [
            node.kind.value,
            node.qualified_name,
            node.name or "",
            norm_path(root, node.file_path) or "",
            _span_json(node) or "",
            json.dumps(node.metadata, sort_keys=True, default=str),
            _span_text(root, node),
        ],
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def vector_project_filter(project_id: str) -> str:
    """A Milvus scalar filter scoping a query/delete to one project.

    Milvus has no parameter binding for filter expressions, so the id is
    inlined; ``project_id`` comes from ``compute_project_id`` (``[A-Za-z0-9_]``
    only), and that invariant is asserted here so a malformed id can never
    smuggle an expression through.
    """
    if not _PID_RE.match(project_id):
        msg = f"unsafe project id for vector filter: {project_id!r}"
        raise ValueError(msg)
    return f'project_id == "{project_id}"'


def norm_path(root: Path, file_path: str | None) -> str | None:
    """Normalize a graphlens file_path to root-relative POSIX form.

    graphlens emits the same physical file inconsistently — definition nodes
    carry an absolute path, FILE nodes a relative one — so without this one
    file would split across spellings (and absolute host paths would leak into
    the stored graph and vectors).
    """
    if not file_path:
        return file_path
    path = Path(file_path)
    if path.is_absolute():
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            return path.as_posix()
    return path.as_posix()


def _chunks(rows: list[dict], size: int) -> Iterator[list[dict]]:
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


def _span_json(node: Node) -> str | None:
    if node.span is None:
        return None
    span = node.span
    return json.dumps(
        [span.start_line, span.start_col, span.end_line, span.end_col],
    )


def _meta_json(metadata: dict[str, object]) -> str:
    return json.dumps(metadata, default=str)


async def ensure_code_schema(store: GraphExecutor) -> None:
    await store.execute(
        "CREATE NODE TABLE IF NOT EXISTS CodeNode("
        "id STRING, project_id STRING, local_id STRING, kind STRING, "
        "qualified_name STRING, name STRING, file_path STRING, "
        "span STRING, metadata STRING, content_hash STRING, "
        "PRIMARY KEY(id))",
    )
    await store.execute(
        "CREATE REL TABLE IF NOT EXISTS Rel("
        "FROM CodeNode TO CodeNode, kind STRING, metadata STRING)",
    )
    # The sha the project's (single, latest-snapshot) graph currently reflects.
    await store.execute(
        "CREATE NODE TABLE IF NOT EXISTS GraphHead("
        "project_id STRING, sha STRING, PRIMARY KEY(project_id))",
    )


async def clear_project(store: GraphStore, project_id: str) -> None:
    """Drop a project's recorded graph head, its nodes, and its edges.

    One transaction, so a crash can't leave a head pointing at deleted nodes
    (which would falsely report AlreadyCurrent for an empty project). The head
    still goes first, which keeps the failure benign on any store that can't
    honour the transaction: nodes-without-a-head make ``graph_head()`` None, so
    ``remote_head`` declines the no-op and the next index self-heals.
    """
    async with store.transaction() as tx:
        await tx.execute(
            "MATCH (h:GraphHead {project_id: $pid}) DELETE h",
            {"pid": project_id},
        )
        await tx.execute(
            "MATCH (n:CodeNode {project_id: $pid}) DETACH DELETE n",
            {"pid": project_id},
        )


async def set_graph_head(
    store: GraphExecutor,
    project_id: str,
    sha: str,
) -> None:
    """Record the sha the project's graph now reflects (last-writer wins)."""
    await store.execute(
        "MERGE (h:GraphHead {project_id: $pid}) SET h.sha = $sha",
        {"pid": project_id, "sha": sha},
    )


async def graph_head(store: GraphExecutor, project_id: str) -> str | None:
    """The sha the project's graph currently reflects, or ``None``.

    The no-op fast path compares against THIS (not "any ref ever indexed"),
    since the graph is a single latest snapshot: after another ref was indexed
    into the same project it holds that ref's sha, so re-indexing the first
    ref must not be skipped.
    """
    rows = await store.execute(
        "MATCH (h:GraphHead {project_id: $pid}) RETURN h.sha AS sha",
        {"pid": project_id},
    )
    return rows[0]["sha"] if rows else None


async def clear_relations(store: GraphExecutor, project_id: str) -> None:
    """Delete a project's edges (keeping its nodes) — for a full edge replace.

    Edges are intra-project, so matching those out of any project node covers
    them all. Incremental re-index replaces edges wholesale (they carry no
    embedding cost) while keeping node/vector work diff-driven.
    """
    await store.execute(
        "MATCH (n:CodeNode {project_id: $p})-[e:Rel]->() DELETE e",
        {"p": project_id},
    )


async def stored_node_hashes(
    store: GraphExecutor,
    project_id: str,
) -> dict[str, str]:
    """The stored ``{local_id: content_hash}`` map — the diff baseline."""
    rows = await store.execute(
        "MATCH (n:CodeNode {project_id: $p}) "
        "RETURN n.local_id AS id, n.content_hash AS h",
        {"p": project_id},
    )
    return {row["id"]: row["h"] for row in rows}


async def delete_nodes(
    store: GraphExecutor,
    project_id: str,
    local_ids: Iterable[str],
) -> None:
    """Detach-delete nodes (and their edges) by local id."""
    rows = [{"id": gid(project_id, lid)} for lid in local_ids]
    for chunk in _chunks(rows, _BATCH):
        await store.execute(
            "UNWIND $rows AS r MATCH (n:CodeNode {id: r.id}) DETACH DELETE n",
            {"rows": chunk},
        )


def vector_ids_filter(gids: list[str]) -> str:
    """A Milvus ``id in [...]`` filter over project-namespaced gids.

    Milvus has no parameter binding for filters, so ids are inlined; each gid's
    charset is asserted so a malformed id can't smuggle an expression through.
    """
    for value in gids:
        if not _GID_RE.match(value):
            msg = f"unsafe gid for vector filter: {value!r}"
            raise ValueError(msg)
    joined = ", ".join(f'"{value}"' for value in gids)
    return f"id in [{joined}]"


async def persist_nodes(
    store: GraphExecutor,
    root: Path,
    project_id: str,
    nodes: Iterable[Node],
) -> None:
    """Batch-upsert nodes with one UNWIND per chunk (thousands/sec)."""
    rows = [
        {
            "id": gid(project_id, node.id),
            "pid": project_id,
            "lid": node.id,
            "kind": node.kind.value,
            "qn": node.qualified_name,
            "name": node.name,
            "fp": norm_path(root, node.file_path),
            "span": _span_json(node),
            "meta": _meta_json(node.metadata),
            "ch": content_hash(root, node),
        }
        for node in nodes
    ]
    for chunk in _chunks(rows, _BATCH):
        await store.execute(
            "UNWIND $rows AS r "
            "MERGE (n:CodeNode {id: r.id}) "
            "SET n.project_id = r.pid, n.local_id = r.lid, n.kind = r.kind, "
            "n.qualified_name = r.qn, n.name = r.name, n.file_path = r.fp, "
            "n.span = r.span, n.metadata = r.meta, n.content_hash = r.ch",
            {"rows": chunk},
        )


async def persist_relations(
    store: GraphExecutor,
    project_id: str,
    relations: Iterable[Relation],
    valid_ids: set[str],
) -> int:
    """Batch-upsert edges whose endpoints were persisted, one per
    (source, target, kind). Returns the deduplicated edge count, so the
    number reported matches what's actually in the graph.
    """
    seen: set[tuple[str, str, str]] = set()
    rows = []
    for relation in relations:
        if relation.source_id not in valid_ids:
            continue
        if relation.target_id not in valid_ids:
            continue
        key = (relation.source_id, relation.target_id, relation.kind.value)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "s": gid(project_id, relation.source_id),
                "t": gid(project_id, relation.target_id),
                "kind": relation.kind.value,
                "meta": _meta_json(relation.metadata),
            },
        )
    for chunk in _chunks(rows, _BATCH):
        await store.execute(
            "UNWIND $rows AS r "
            "MATCH (a:CodeNode {id: r.s}), (b:CodeNode {id: r.t}) "
            "MERGE (a)-[e:Rel {kind: r.kind}]->(b) SET e.metadata = r.meta",
            {"rows": chunk},
        )
    return len(rows)
