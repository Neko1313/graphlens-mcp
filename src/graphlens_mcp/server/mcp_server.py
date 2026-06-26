"""FastMCP server with 8 graph navigation tools."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

from graphlens_mcp.indexer.workspace import Workspace
from graphlens_mcp.server.models import FileStructureResult, GraphResult, NodeInfoResult
from graphlens_mcp.server.tools import (
    tool_find_references,
    tool_get_callees,
    tool_get_callers,
    tool_get_cross_language_calls,
    tool_get_file_structure,
    tool_get_neighbors,
    tool_get_node_info,
    tool_search_symbols,
)
from graphlens_mcp.store.sqlite_store import SqliteStore

logger = logging.getLogger(__name__)

# Parameter constraints shared across tools (validated by FastMCP/pydantic).
Limit = Annotated[int, Field(ge=1, le=200, description="Max nodes to return")]
Depth = Annotated[int, Field(ge=1, le=10, description="Max traversal hops")]
NeighborDepth = Annotated[int, Field(ge=1, le=5, description="Max neighbor hops")]


def create_mcp(store: SqliteStore, workspace: Workspace) -> FastMCP:
    """Build the FastMCP server exposing the graph navigation tools."""
    mcp = FastMCP(
        "graphlens",
        instructions=(
            "Semantic code graph for the current project. "
            "Use search_symbols first, then navigate with get_callers/get_callees. "
            "Each response includes resolver_status: ok|degraded|skeleton — "
            "treat skeleton/degraded results as approximate."
        ),
    )

    @mcp.tool(
        description=(
            "Search for symbols by name using full-text search. "
            "ALWAYS start here when you need to find a symbol. "
            "Returns node IDs for use with other tools. "
            "Supports FTS5 prefix syntax, e.g. 'create_order*'."
        )
    )
    async def search_symbols(query: str, limit: Limit = 20) -> GraphResult:
        return await tool_search_symbols(store, query, limit)

    @mcp.tool(
        description=(
            "Get full info for a node: source snippet, signature, kind, file location. "
            "Use after search_symbols when you need to read a specific symbol's implementation."
        )
    )
    async def get_node_info(node_id: str) -> NodeInfoResult:
        return await tool_get_node_info(store, workspace, node_id)

    @mcp.tool(
        description=(
            "Get the symbol outline of a file (classes, functions, methods). "
            "Use instead of reading the whole file when you only need structure."
        )
    )
    async def get_file_structure(path: str, limit: Limit = 200) -> FileStructureResult:
        return await tool_get_file_structure(store, workspace, path, limit)

    @mcp.tool(
        description=(
            "Return nodes that node_id CALLS (outgoing, up to max_depth hops). "
            "Use to understand what a function depends on internally."
        )
    )
    async def get_callees(node_id: str, max_depth: Depth = 3, limit: Limit = 200) -> GraphResult:
        return await tool_get_callees(store, workspace, node_id, max_depth, limit)

    @mcp.tool(
        description=(
            "Return nodes that CALL node_id (incoming, up to max_depth hops). "
            "PRIMARY tool for impact analysis: 'what breaks if I change X?' "
            "Walk callers to find the full call chain before touching shared code."
        )
    )
    async def get_callers(node_id: str, max_depth: Depth = 3, limit: Limit = 200) -> GraphResult:
        return await tool_get_callers(store, workspace, node_id, max_depth, limit)

    @mcp.tool(
        description=(
            "Return nodes within depth hops in ANY direction. "
            "Use to explore context around an unknown symbol."
        )
    )
    async def get_neighbors(
        node_id: str, depth: NeighborDepth = 2, limit: Limit = 200
    ) -> GraphResult:
        return await tool_get_neighbors(store, workspace, node_id, depth, limit)

    @mcp.tool(
        description=(
            "Return nodes that REFERENCE node_id (type annotations, assignments, non-call usages). "
            "Use alongside get_callers for complete impact analysis."
        )
    )
    async def find_references(node_id: str, limit: Limit = 200) -> GraphResult:
        return await tool_find_references(store, workspace, node_id, limit)

    @mcp.tool(
        description=(
            "Return nodes in OTHER languages that communicate with node_id via shared boundaries "
            "(HTTP routes, gRPC, queues). Shows cross-service connections."
        )
    )
    async def get_cross_language_calls(node_id: str, limit: Limit = 200) -> GraphResult:
        return await tool_get_cross_language_calls(store, workspace, node_id, limit)

    return mcp


def run_server(db_path: Path, project_root: Path) -> None:
    """Entry point for `graphlens-mcp serve`."""

    async def _main() -> None:
        workspace = await Workspace.create(project_root, db_path)
        mcp = create_mcp(workspace.store, workspace)
        try:
            await mcp.run_stdio_async()
        finally:
            # Release the DB connection and shut down resolver/LSP processes on exit.
            await workspace.close()

    asyncio.run(_main())
