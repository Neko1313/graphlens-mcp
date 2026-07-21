from mcp.server import MCPServer

from features.relations.adapter import relations_resource, relations_tool


def register(mcp: MCPServer) -> None:
    """Register the relations feature: the relations tool + resource."""
    mcp.tool()(relations_tool)
    mcp.resource(
        "graphlens://{project}/node/{id}/relations{?depth,limit,kinds}",
        mime_type="application/json",
    )(relations_resource)
