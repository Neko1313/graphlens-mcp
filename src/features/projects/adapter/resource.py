from collections.abc import Awaitable, Callable
from contextlib import suppress

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ResourceNotFoundError
from mcp.server.mcpserver.resources.types import FunctionResource

from entities.project import Project
from features.projects import service
from shared.common.context import AppContext
from shared.common.db.registry import get_registry_store

__all__ = [
    "hydrate_project_resources",
    "notify_resources_changed",
    "register_project_resource",
    "unregister_project_resource",
]


def _project_uri(project_id: str) -> str:
    return f"project://{project_id}"


def _reader(
    project_id: str,
) -> Callable[[], Awaitable[dict[str, object]]]:
    """A no-arg async reader for one project's metadata resource."""

    async def read() -> dict[str, object]:
        project = await service.get_project(get_registry_store(), project_id)
        if project is None:
            msg = f"unknown project: {project_id}"
            raise ResourceNotFoundError(msg)
        return project.model_dump(mode="json")

    return read


def register_project_resource(server: MCPServer, project: Project) -> None:
    """Register (or refresh) a static resource for one project.

    Idempotent: an existing entry for the same id is replaced so a re-index
    picks up a changed name/description.
    """
    unregister_project_resource(server, project.id)
    server.add_resource(
        FunctionResource.from_function(
            _reader(project.id),
            uri=_project_uri(project.id),
            name=project.name,
            title=project.name,
            description=(
                project.description or f"Indexed project {project.name}"
            ),
            mime_type="application/json",
        ),
    )


def unregister_project_resource(server: MCPServer, project_id: str) -> None:
    """Drop a project's static resource.

    The SDK exposes ``remove_tool``/``remove_prompt`` but no resource removal,
    so reach into the manager directly — the one place that does.
    """
    server._resource_manager._resources.pop(_project_uri(project_id), None)


async def hydrate_project_resources(server: MCPServer) -> None:
    """Register a resource for every already-indexed project (startup)."""
    for project in await service.list_projects(get_registry_store()):
        register_project_resource(server, project)


async def notify_resources_changed(ctx: Context[AppContext]) -> None:
    """Nudge the client to refetch the resource list, on both transports."""
    with suppress(Exception):
        await ctx.request_context.session.send_resource_list_changed()
    with suppress(Exception):
        await ctx.notify_resources_changed()
