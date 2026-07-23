import argparse

from app.server import mcp

__all__ = ["main"]


def main() -> None:
    """Run the graphlens MCP server.

    Defaults to stdio — the common local case, where an agent spawns the
    process and talks over the pipe. Pass ``--http`` to serve over Streamable
    HTTP for a standalone or k8s deployment.
    """
    parser = argparse.ArgumentParser(
        prog="graphlens-mcp",
        description="graphlens semantic code-graph MCP server",
    )
    parser.add_argument(
        "--http",
        action="store_true",
        help="Serve over Streamable HTTP instead of stdio.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind host for --http (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Bind port for --http (default: 8000).",
    )
    args = parser.parse_args()

    if args.http:
        mcp.run(transport="streamable-http", host=args.host, port=args.port)
    else:
        mcp.run()
