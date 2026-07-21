from mcp.server import MCPServer

from features.relations.adapter import relations


def register(mcp: MCPServer) -> None:
    """Register the relations feature: a node's callers/callees resource."""
    mcp.resource(
        "graphlens://{project}/node/{id}/relations{?depth,limit,kinds}",
        mime_type="application/json",
    )(relations)
