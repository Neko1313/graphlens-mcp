from mcp.server import MCPServer

from app.lifespan import lifespan
from features.info import register as register_info
from features.projects import register as register_projects
from features.relations import register as register_relations
from features.search import register as register_search
from shared.common.context import AppContext

mcp = MCPServer[AppContext]("graphlens", lifespan=lifespan)

register_projects(mcp)
register_search(mcp)
register_info(mcp)
register_relations(mcp)

app = mcp.streamable_http_app()
