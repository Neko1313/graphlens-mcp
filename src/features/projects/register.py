from mcp.server import MCPServer

from features.projects.adapter import (
    get_project,
    index_project,
    list_projects_resource,
    list_projects_tool,
    refresh_project,
    remove_project,
)


def register(mcp: MCPServer) -> None:
    """Register the projects feature: management tools + list/get resources."""
    mcp.tool()(index_project)
    mcp.tool()(refresh_project)
    mcp.tool()(remove_project)
    mcp.tool(name="list_projects")(list_projects_tool)
    mcp.resource(
        "projects://list",
        mime_type="application/json",
    )(list_projects_resource)
    mcp.resource(
        "project://{project_id}",
        mime_type="application/json",
    )(get_project)
