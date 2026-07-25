from mcp.server import MCPServer

from features.navigation.adapter import (
    deadcode,
    find,
    impact,
    map_area,
    trace,
    xflow,
)


def register(mcp: MCPServer) -> None:
    """Register the navigation feature: the 6 code-navigation prompts."""
    mcp.prompt()(impact)
    mcp.prompt()(find)
    mcp.prompt()(trace)
    mcp.prompt(name="map")(map_area)
    mcp.prompt()(xflow)
    mcp.prompt()(deadcode)
