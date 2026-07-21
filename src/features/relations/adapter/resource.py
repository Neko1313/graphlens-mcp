from mcp.server.mcpserver.exceptions import ResourceNotFoundError

from features.relations import service
from shared.common.db.graph import get_graph_store

__all__ = ["relations"]


async def relations(
    project: str,
    id: str,  # noqa: A002
    depth: int = 2,
    limit: int = 25,
    kinds: str = "",
) -> dict[str, object]:
    """Callers and callees of a symbol (see ``get_relations``)."""
    result = await service.get_relations(
        get_graph_store(), project, id, depth, limit, kinds,
    )
    if result is None:
        msg = f"unknown node {id} in project {project}"
        raise ResourceNotFoundError(msg)
    return result.model_dump()
