from mcp.server import MCPServer

from features.info.adapter import add, greeting, summarize


def register(mcp: MCPServer) -> None:
    """Register MCPServer."""
    mcp.tool()(add)
    mcp.resource("greeting://{name}")(greeting)
    mcp.prompt()(summarize)
