import json

from shared.common import source
from shared.common.db.graph import GraphStore
from shared.common.db.registry import ProjectRegistry

__all__ = ["get_file_outline", "get_file_source", "get_node"]

_OUTLINE_KINDS = ("class", "function", "method")


def _start_line(span_json: str | None) -> int:
    return json.loads(span_json)[0] if span_json else 0


async def get_node(
    graph_store: GraphStore,
    registry: ProjectRegistry,
    project_id: str,
    node_id: str,
) -> dict[str, object] | None:
    """A node's source + signature + metadata, or None if it isn't indexed."""
    rows = await graph_store.execute(
        "MATCH (n:CodeNode {project_id: $p, local_id: $id}) "
        "RETURN n.local_id AS id, n.name AS name, n.qualified_name AS qn, "
        "n.kind AS kind, n.file_path AS file_path, n.span AS span, "
        "n.metadata AS metadata LIMIT 1",
        {"p": project_id, "id": node_id},
    )
    if not rows:
        return None
    node = rows[0]
    src, signature = "", ""
    project = await registry.get(project_id)
    if project is not None:
        src, signature = await source.read_span(
            project.path, node["file_path"], node["span"],
        )
    return {
        "id": node["id"],
        "name": node["name"],
        "qualified_name": node["qn"],
        "kind": node["kind"],
        "file_path": node["file_path"],
        "signature": signature,
        "source": src,
        "metadata": json.loads(node["metadata"]) if node["metadata"] else {},
    }


async def get_file_source(
    registry: ProjectRegistry,
    project_id: str,
    path: str,
) -> str | None:
    """The full text of a project file, or None if unreadable/unknown."""
    project = await registry.get(project_id)
    if project is None:
        return None
    return await source.read_file(project.path, path)


async def get_file_outline(
    graph_store: GraphStore,
    registry: ProjectRegistry,
    project_id: str,
    path: str,
) -> list[dict[str, object]] | None:
    """The classes/functions/methods declared in a file, ordered by line.

    None if the project or file is unknown (distinct from a real file that
    simply declares no symbols, which is an empty list).
    """
    project = await registry.get(project_id)
    if project is None or not await source.is_file(project.path, path):
        return None
    rows = await graph_store.execute(
        "MATCH (n:CodeNode {project_id: $p, file_path: $path}) "
        "WHERE n.kind IN $kinds "
        "RETURN n.local_id AS id, n.name AS name, "
        "n.qualified_name AS qn, n.kind AS kind, n.span AS span",
        {"p": project_id, "path": path, "kinds": list(_OUTLINE_KINDS)},
    )
    rows.sort(key=lambda row: _start_line(row["span"]))
    return [
        {
            "id": row["id"],
            "name": row["name"],
            "qualified_name": row["qn"],
            "kind": row["kind"],
            "line": _start_line(row["span"]),
        }
        for row in rows
    ]
