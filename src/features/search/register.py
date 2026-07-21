from mcp.server import MCPServer

from features.search.adapter import search


def register(mcp: MCPServer) -> None:
    """Register the search feature: the semantic/name search tool.

    ``structured_output=False`` — the tool returns content blocks
    (ResourceLinks / text), not a JSON object to schematize.
    """
    mcp.tool(structured_output=False)(search)
