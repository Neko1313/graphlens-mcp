import re
from pathlib import PurePosixPath

from shared.common import source
from shared.common.db.graph import GraphStore
from shared.common.db.registry import ProjectRegistry
from shared.common.db.vector import VectorStore
from shared.common.indexing.embed import encode

__all__ = ["get_node_source", "resolve_project", "search"]

_MAX_LIMIT = 100
_SAFE_PREFIX = re.compile(r"^[\w./-]*$")


async def resolve_project(
    registry: ProjectRegistry,
    project_id: str | None,
) -> str:
    """Pick the project to search: the given id, or the sole indexed one."""
    if project_id:
        if await registry.get(project_id) is None:
            msg = f"unknown project: {project_id}"
            raise ValueError(msg)
        return project_id
    projects = await registry.list_all()
    if len(projects) == 1:
        return projects[0].id
    if not projects:
        msg = "no projects indexed yet; run index_project first"
        raise ValueError(msg)
    ids = ", ".join(p.id for p in projects)
    msg = f"multiple projects indexed; pass project= one of: {ids}"
    raise ValueError(msg)


def _glob_prefix_filter(path_glob: str) -> str | None:
    """A Milvus ``like`` filter for the glob's literal directory prefix.

    Pushes scope into the vector search so an in-scope hit can't be lost past
    the fetch window. Only built when the prefix is path-safe, keeping the
    inlined filter injection-free; ``None`` otherwise (Python glob still runs).
    """
    prefix = re.split(r"[*?\[]", path_glob, maxsplit=1)[0]
    prefix = prefix.rsplit("/", 1)[0] + "/" if "/" in prefix else ""
    if not prefix or not _SAFE_PREFIX.match(prefix):
        return None
    return f'file_path like "{prefix}%"'


def _in_scope(file_path: str, path_glob: str) -> bool:
    return PurePosixPath(file_path).full_match(path_glob)


async def search(
    graph_store: GraphStore,
    vector_store: VectorStore,
    query: str,
    project_id: str,
    limit: int = 25,
    path_glob: str | None = None,
) -> list[dict[str, object]]:
    """Semantic (embedding) hits blended with name-substring matches."""
    query = query.strip()
    if not query:
        return []
    limit = max(1, min(limit, _MAX_LIMIT))
    filter_expr = _glob_prefix_filter(path_glob) if path_glob else None
    # If the prefix filter narrows scope server-side, `limit` candidates
    # suffice; otherwise over-fetch and refine the glob in Python.
    over_fetch = limit if filter_expr or not path_glob else limit * 10
    hits = await vector_store.search(
        project_id,
        encode([query])[0].tolist(),
        limit=max(over_fetch, limit),
        filter_expr=filter_expr,
        output_fields=["name", "kind", "file_path"],
    )
    results: list[dict[str, object]] = []
    seen: set[str] = set()
    for hit in hits:
        entity = hit.get("entity", {})
        file_path = entity.get("file_path") or ""
        if path_glob and not _in_scope(file_path, path_glob):
            continue
        node_id = hit["id"]
        if node_id in seen:
            continue
        seen.add(node_id)
        results.append(
            {
                "id": node_id,
                "name": entity.get("name"),
                "kind": entity.get("kind"),
                "file_path": file_path,
                "score": round(float(hit.get("distance", 0.0)), 4),
                "match": "semantic",
            },
        )
        if len(results) >= limit:
            return results

    name_rows = await graph_store.execute(
        "MATCH (n:CodeNode {project_id: $p}) "
        "WHERE contains(lower(n.name), lower($q)) "
        "RETURN n.local_id AS id, n.name AS name, n.kind AS kind, "
        "n.file_path AS file_path LIMIT $lim",
        {"p": project_id, "q": query, "lim": (limit - len(results)) * 3},
    )
    for row in name_rows:
        node_id = row["id"]
        file_path = row["file_path"] or ""
        if node_id in seen:
            continue
        if path_glob and not _in_scope(file_path, path_glob):
            continue
        seen.add(node_id)
        results.append(
            {
                "id": node_id,
                "name": row["name"],
                "kind": row["kind"],
                "file_path": file_path,
                "score": None,
                "match": "name",
            },
        )
        if len(results) >= limit:
            break
    return results


async def get_node_source(
    graph_store: GraphStore,
    registry: ProjectRegistry,
    project_id: str,
    node_id: str,
) -> tuple[str, str]:
    """``(source, signature)`` for a hit, for detailed search output."""
    rows = await graph_store.execute(
        "MATCH (n:CodeNode {project_id: $p, local_id: $id}) "
        "RETURN n.file_path AS file_path, n.span AS span LIMIT 1",
        {"p": project_id, "id": node_id},
    )
    if not rows:
        return "", ""
    project = await registry.get(project_id)
    if project is None:
        return "", ""
    return await source.read_span(
        project.path, rows[0]["file_path"], rows[0]["span"],
    )
