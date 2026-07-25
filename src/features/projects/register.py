from mcp.server import MCPServer

from features.projects.adapter import (
    index,
    list_projects,
    remove_project,
)


def register(mcp: MCPServer) -> None:
    """Register the projects feature's management tools.

    One ``index`` tool (local directory or remote repo_url), plus list/remove.
    Per-project resources aren't registered here: each indexed project becomes
    a static ``project://{id}`` resource, added/removed at runtime by the
    tools and hydrated from the registry at startup (see the lifespan).
    """
    mcp.tool()(index)
    mcp.tool()(remove_project)
    mcp.tool()(list_projects)
