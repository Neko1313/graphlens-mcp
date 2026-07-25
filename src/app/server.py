"""The MCP server — one binary, two deployment modes.

**Local (zero-infra).** No DSN set: embedded Kuzu + Milvus Lite on disk, a
single trusted user, no auth. Run ``graphlens-mcp`` (stdio) — the agent spawns
it. Confirmations use MCP elicitation.

**Server (multi-tenant k8s).** ``DB__GRAPH`` (a ``neo4j://`` DSN) and
``DB__VECTOR`` select the host backends behind the same ports — no code change.
Run ``graphlens-mcp --http`` behind an ingress. Authorization is pushed
outward, on two axes:

- **Write / index** is gated by the git token: no valid ``ci_token`` -> no
  clone/pull -> nothing written for that repo. That token *is* the write ACL,
  so the server adds no second layer.
- **Read / query** is not checked here. It is regulated at the transport by an
  external **OIDC gateway** (e.g. Casdoor) placed in front of the server — a
  deployment concern, deliberately out of this project's scope. The server
  builds no session state (the Streamable-HTTP ``Mcp-Session-Id`` is optional);
  per-agent identity, if ever needed, comes from the gateway's auth principal.
"""

from mcp.server import MCPServer

from app.instructions import INSTRUCTIONS
from app.lifespan import lifespan
from features.info import register as register_info
from features.navigation import register as register_navigation
from features.projects import register as register_projects
from features.relations import register as register_relations
from features.search import register as register_search
from shared.common.context import AppContext

mcp = MCPServer[AppContext](
    "graphlens",
    instructions=INSTRUCTIONS,
    lifespan=lifespan,
)

register_projects(mcp)
register_search(mcp)
register_info(mcp)
register_relations(mcp)
register_navigation(mcp)

app = mcp.streamable_http_app()
