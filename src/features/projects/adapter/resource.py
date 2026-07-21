from mcp.server.mcpserver.exceptions import ResourceNotFoundError

from features.projects import service
from shared.common.db.registry import get_registry_store

__all__ = ["get_project", "list_projects"]


async def list_projects() -> list[dict[str, object]]:
    """List every indexed project so the agent knows what's available."""
    projects = await service.list_projects(get_registry_store())
    return [project.model_dump(mode="json") for project in projects]


async def get_project(project_id: str) -> dict[str, object]:
    """One project's metadata: path, git url, name, and description."""
    project = await service.get_project(get_registry_store(), project_id)
    if project is None:
        msg = f"unknown project: {project_id}"
        raise ResourceNotFoundError(msg)
    return project.model_dump(mode="json")
