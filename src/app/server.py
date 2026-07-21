from mcp.server import MCPServer

from app.lifespan import lifespan
from features.projects import register as register_projects
from features.search import register as register_search
from shared.common.context import AppContext

mcp = MCPServer[AppContext]("graphlens", lifespan=lifespan)

register_search(mcp)
register_projects(mcp)

app = mcp.streamable_http_app()
