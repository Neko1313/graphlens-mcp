from typing import Literal

from mcp_types import ResourceLink, TextContent

from features.search import service
from shared.common.db.graph import get_graph_store
from shared.common.db.registry import get_registry_store
from shared.common.db.vector import get_vector_store

__all__ = ["search"]

_DETAIL_SOURCE_CAP = 1200


async def search(
    query: str,
    project: str | None = None,
    limit: int = 25,
    path_glob: str | None = None,
    verbosity: Literal["concise", "detailed"] = "concise",
) -> list[TextContent | ResourceLink]:
    """Find symbols by meaning or name across an indexed project.

    Blends semantic (embedding) search with name-substring matches. ``concise``
    returns each hit as a link to its ``graphlens://…/node/{id}`` resource
    (follow it for the full source); ``detailed`` inlines each symbol's source.
    Scope with ``path_glob`` (e.g. ``src/**/*.py``). Pass ``project`` when more
    than one is indexed. Does NOT index — run ``index_project`` first.
    """
    graph_store = get_graph_store()
    project_id = await service.resolve_project(get_registry_store(), project)
    hits = await service.search(
        graph_store, get_vector_store(), query, project_id, limit, path_glob,
    )

    blocks: list[TextContent | ResourceLink] = [
        TextContent(
            type="text",
            text=f"{len(hits)} results for {query!r} in {project_id}",
        ),
    ]
    for hit in hits:
        uri = f"graphlens://{project_id}/node/{hit['id']}"
        label = f"{hit['kind']} · {hit['file_path']}"
        if verbosity == "detailed":
            src, _ = await service.get_node_source(
                graph_store, get_registry_store(), project_id, str(hit["id"]),
            )
            blocks.append(
                TextContent(
                    type="text",
                    text=(
                        f"{hit['name']} — {label}\n"
                        f"{src[:_DETAIL_SOURCE_CAP]}\n[{uri}]"
                    ),
                ),
            )
        else:
            blocks.append(
                ResourceLink(
                    type="resource_link",
                    name=str(hit["name"]),
                    uri=uri,
                    description=label,
                    mime_type="text/x-python",
                ),
            )
    return blocks
