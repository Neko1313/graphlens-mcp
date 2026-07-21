from mcp.server.mcpserver.exceptions import ResourceNotFoundError

from features.info import service
from shared.common.db.graph import get_graph_store
from shared.common.db.registry import get_registry_store

__all__ = ["node", "source_file", "source_file_outline"]


async def node(project: str, id: str) -> dict[str, object]:  # noqa: A002
    """Read one symbol: its source, signature, kind, and metadata."""
    result = await service.get_node(
        get_graph_store(), get_registry_store(), project, id,
    )
    if result is None:
        msg = f"unknown node {id} in project {project}"
        raise ResourceNotFoundError(msg)
    return result


async def source_file(project: str, path: str) -> str:
    """Read a file's full source."""
    text = await service.get_file_source(get_registry_store(), project, path)
    if text is None:
        msg = f"file not found: {path} in project {project}"
        raise ResourceNotFoundError(msg)
    return text


async def source_file_outline(
    project: str,
    path: str,
) -> list[dict[str, object]]:
    """Read a file's outline: its symbols and their line numbers."""
    result = await service.get_file_outline(
        get_graph_store(), get_registry_store(), project, path,
    )
    if result is None:
        msg = f"file not found: {path} in project {project}"
        raise ResourceNotFoundError(msg)
    return result
