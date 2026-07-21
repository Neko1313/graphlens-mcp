from typing import Literal

from mcp_types import ResourceLink, TextContent

from features.search import service
from shared.common.db.graph import get_graph_store
from shared.common.db.registry import get_registry_store, resolve_project
from shared.common.db.vector import get_vector_store

__all__ = ["search"]

_DETAIL_SOURCE_CAP = 1200


def _label(kind: str, signature: str, file_path: str, line: int | None) -> str:
    head = f"{kind} {signature}".strip()
    where = f"{file_path}:{line}" if line else file_path
    return f"{head} · {where}"


async def search(
    query: str,
    project: str | None = None,
    limit: int = 25,
    path_glob: str | None = None,
    verbosity: Literal["concise", "detailed"] = "concise",
    exhaustive: bool = False,
) -> list[TextContent | ResourceLink]:
    """Find symbols by meaning, name, or literal content across a project.

    Blends semantic (embedding), name-substring, and literal content search.
    ``concise`` returns each hit's signature plus a link to its resource
    (follow it for the full source); ``detailed`` inlines the source.
    ``exhaustive`` lists every in-scope file path instead. Scope with
    ``path_glob`` (e.g. ``src/**/*.py``). Pass ``project`` when more than one
    is indexed. Does NOT index — run ``index_project`` first.
    """
    graph_store = get_graph_store()
    registry = get_registry_store()
    project_id = await resolve_project(registry, project)
    hits = await service.search(
        graph_store, get_vector_store(), registry, query, project_id,
        limit, path_glob, exhaustive=exhaustive,
    )

    blocks: list[TextContent | ResourceLink] = [
        TextContent(
            type="text",
            text=f"{len(hits)} results for {query!r} in {project_id}",
        ),
    ]
    for hit in hits:
        label = _label(hit.kind, hit.signature, hit.file_path, hit.line)
        if verbosity == "detailed" and hit.id:
            src, _ = await service.get_node_source(
                graph_store, registry, project_id, hit.id,
            )
            blocks.append(
                TextContent(
                    type="text",
                    text=(
                        f"{hit.name} — {label}\n"
                        f"{src[:_DETAIL_SOURCE_CAP]}\n[{hit.uri}]"
                    ),
                ),
            )
        else:
            blocks.append(
                ResourceLink(
                    type="resource_link", name=hit.name, uri=hit.uri,
                    description=label, mime_type="text/x-python",
                ),
            )
    return blocks
