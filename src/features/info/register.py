from mcp.server import MCPServer

from features.info.adapter import (
    info,
    node,
    source_file,
    source_file_outline,
)


def register(mcp: MCPServer) -> None:
    """Register the info feature: the info tool + node/file resources.

    The outline template is registered before the plain file template: its
    URI is more specific (``…/file/{+path}/outline``), and ``{+path}`` is
    greedy, so it must be matched first.
    """
    mcp.tool()(info)
    mcp.resource(
        "graphlens://{project}/node/{id}",
        mime_type="application/json",
    )(node)
    mcp.resource(
        "graphlens://{project}/file/{+path}/outline",
        mime_type="application/json",
    )(source_file_outline)
    mcp.resource(
        "graphlens://{project}/file/{+path}",
        mime_type="application/json",
    )(source_file)
