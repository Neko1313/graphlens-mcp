from mcp.server import MCPServer

from features.projects.adapter import add, summarize, greeting


def register(mcp: MCPServer) -> None:
    """Register MCPServer."""
    mcp.tool()(add)
    mcp.resource("greeting://{name}")(greeting)
    mcp.prompt()(summarize)
