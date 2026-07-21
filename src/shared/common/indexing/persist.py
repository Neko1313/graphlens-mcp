import json
from collections.abc import Iterable, Iterator
from pathlib import Path

from graphlens import Node, Relation

from shared.common.db.graph import GraphStore

__all__ = [
    "clear_project",
    "ensure_code_schema",
    "gid",
    "norm_path",
    "persist_nodes",
    "persist_relations",
]

_BATCH = 500


def gid(project_id: str, node_id: str) -> str:
    """Namespace a graphlens node id by project so ids never collide across
    projects sharing the one code-graph database.
    """
    return f"{project_id}::{node_id}"


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


async def ensure_code_schema(store: GraphStore) -> None:
    await store.execute(
        "CREATE NODE TABLE IF NOT EXISTS CodeNode("
        "id STRING, project_id STRING, local_id STRING, kind STRING, "
        "qualified_name STRING, name STRING, file_path STRING, "
        "span STRING, metadata STRING, PRIMARY KEY(id))",
    )
    await store.execute(
        "CREATE REL TABLE IF NOT EXISTS Rel("
        "FROM CodeNode TO CodeNode, kind STRING, metadata STRING)",
    )


async def clear_project(store: GraphStore, project_id: str) -> None:
    """Drop a project's nodes (and edges) so a re-index starts clean."""
    await store.execute(
        "MATCH (n:CodeNode {project_id: $pid}) DETACH DELETE n",
        {"pid": project_id},
    )


async def persist_nodes(
    store: GraphStore,
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
        }
        for node in nodes
    ]
    for chunk in _chunks(rows, _BATCH):
        await store.execute(
            "UNWIND $rows AS r "
            "MERGE (n:CodeNode {id: r.id}) "
            "SET n.project_id = r.pid, n.local_id = r.lid, n.kind = r.kind, "
            "n.qualified_name = r.qn, n.name = r.name, n.file_path = r.fp, "
            "n.span = r.span, n.metadata = r.meta",
            {"rows": chunk},
        )


async def persist_relations(
    store: GraphStore,
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
