from mcp.server import MCPServer

from features.projects.adapter import get_project, index_project, list_projects


def register(mcp: MCPServer) -> None:
    """Register the projects feature: index tool + list/get resources."""
    mcp.tool()(index_project)
    mcp.resource(
        "projects://list",
        mime_type="application/json",
    )(list_projects)
    mcp.resource(
        "project://{project_id}",
        mime_type="application/json",
    )(get_project)
