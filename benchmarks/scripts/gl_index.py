"""
Build one target's graphlens index — the benchmark's pre-index step.

graphlens exposes no index CLI: indexing is the ``index`` MCP tool. Rather than
shelling out to a server we don't have, this drives the tool through an
in-process MCP client, which is exactly the path an agent takes.

    XDG_DATA_HOME=<per-project store> python scripts/gl_index.py <repo path>

Runs under the *graphlens* project environment (`uv run --project ..`), not the
benchmark venv — it imports the server package under test. Prints the tool's
JSON summary (files / nodes / relations / resolver_status) on the last line, so
`bench.setup.build_index` can record it and spot a degraded index.
"""

from __future__ import annotations

import asyncio
import json
import sys


async def index(path: str) -> int:
    from app.server import mcp  # noqa: PLC0415 — needs the graphlens env
    from mcp import Client  # noqa: PLC0415

    async with Client(mcp) as client:
        result = await client.call_tool("index", {"directory": path})
        if result.is_error:
            for block in result.content:
                print(getattr(block, "text", block), file=sys.stderr)
            return 1
        # `index` returns its result wrapped as {"result": {...}} (structured
        # output); unwrap so the recorded stats are the summary itself.
        payload = result.structured_content or {}
        print(json.dumps(payload.get("result", payload), default=str))
        return 0


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <repo path>", file=sys.stderr)
        return 2
    return asyncio.run(index(sys.argv[1]))


if __name__ == "__main__":
    sys.exit(main())
