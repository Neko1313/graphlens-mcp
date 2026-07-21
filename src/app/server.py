from mcp.server import MCPServer

from features.search import register as register_search

mcp = MCPServer("graphlens")

register_search(mcp)

app = mcp.streamable_http_app()
