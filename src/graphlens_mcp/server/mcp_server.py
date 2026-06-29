"""FastMCP server: graph navigation + optional semantic search/clusters."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Annotated

from fastmcp import FastMCP
from pydantic import Field

from graphlens_mcp.indexer.workspace import Workspace

# Runtime import (NOT under TYPE_CHECKING): with `from __future__ import
# annotations` every annotation is a string, and FastMCP evaluates each tool's
# return annotation at registration time to build its output schema. Under
# TYPE_CHECKING that eval raises NameError and the server fails to start, so
# the agent reports it "cannot connect". noqa: TC001 stops ruff re-hiding it.
from graphlens_mcp.server.models import (  # noqa: TC001
    ClusterInfo,
    ClusterList,
    CodeSearchResult,
    FileStructureResult,
    GraphResult,
    NodeInfoResult,
    SemanticResult,
)
from graphlens_mcp.server.tools import (
    tool_find_references,
    tool_find_related,
    tool_get_callees,
    tool_get_callers,
    tool_get_cluster,
    tool_get_cross_language_calls,
    tool_get_file_structure,
    tool_get_neighbors,
    tool_get_node_info,
    tool_list_clusters,
    tool_search_code,
    tool_search_semantic,
    tool_search_symbols,
)

if TYPE_CHECKING:
    from pathlib import Path

    from graphlens_mcp.store.sqlite_store import SqliteStore

logger = logging.getLogger(__name__)

# Parameter constraints shared across tools (validated by FastMCP/pydantic).
Limit = Annotated[int, Field(ge=1, le=200, description="Max nodes to return")]
Depth = Annotated[int, Field(ge=1, le=10, description="Max traversal hops")]
NeighborDepth = Annotated[
    int, Field(ge=1, le=5, description="Max neighbor hops")
]


def create_mcp(store: SqliteStore, workspace: Workspace) -> FastMCP:
    """Build the FastMCP server exposing the graph navigation tools."""
    mcp = FastMCP(
        "graphlens",
        instructions=(
            "Semantic code graph for the current project — prefer these "
            "tools over raw grep/file reads.\n"
            "- Know the name? search_symbols, then get_callers/get_callees "
            "for impact analysis.\n"
            "- Describe behavior? search_semantic finds code by meaning and "
            "returns node ids to pivot into the graph.\n"
            "- Raw text (strings, logs, comments, config)? search_code is the "
            "grep replacement.\n"
            "- Orienting in an unfamiliar repo? list_clusters / get_cluster "
            "map semantic zones; find_related finds similar code.\n"
            "Each graph response includes resolver_status: ok|degraded — "
            "treat degraded results as approximate. Semantic tools report "
            "available=false when the optional [semantic] extra is missing."
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
            "Get full info for a node: source snippet, signature, "
            "kind, file location. "
            "Use after search_symbols when you need to read a "
            "specific symbol's implementation."
        )
    )
    async def get_node_info(node_id: str) -> NodeInfoResult:
        return await tool_get_node_info(store, workspace, node_id)

    @mcp.tool(
        description=(
            "Get the symbol outline of a file (classes, functions, methods). "
            "Use instead of reading the whole file when you only "
            "need structure."
        )
    )
    async def get_file_structure(
        path: str, limit: Limit = 200
    ) -> FileStructureResult:
        return await tool_get_file_structure(store, workspace, path, limit)

    @mcp.tool(
        description=(
            "Return nodes that node_id CALLS (outgoing, up to "
            "max_depth hops). "
            "Use to understand what a function depends on "
            "internally."
        )
    )
    async def get_callees(
        node_id: str, max_depth: Depth = 3, limit: Limit = 200
    ) -> GraphResult:
        return await tool_get_callees(
            store, workspace, node_id, max_depth, limit
        )

    @mcp.tool(
        description=(
            "Return nodes that CALL node_id (incoming, up to max_depth hops). "
            "PRIMARY tool for impact analysis: "
            "'what breaks if I change X?' "
            "Walk callers to find the full call chain before "
            "touching shared code."
        )
    )
    async def get_callers(
        node_id: str, max_depth: Depth = 3, limit: Limit = 200
    ) -> GraphResult:
        return await tool_get_callers(
            store, workspace, node_id, max_depth, limit
        )

    @mcp.tool(
        description=(
            "Return nodes within depth hops in ANY direction. "
            "Use to explore context around an unknown symbol."
        )
    )
    async def get_neighbors(
        node_id: str, depth: NeighborDepth = 2, limit: Limit = 200
    ) -> GraphResult:
        return await tool_get_neighbors(
            store, workspace, node_id, depth, limit
        )

    @mcp.tool(
        description=(
            "Return nodes that REFERENCE node_id (type annotations, "
            "assignments, non-call usages). "
            "Use alongside get_callers for complete impact analysis."
        )
    )
    async def find_references(node_id: str, limit: Limit = 200) -> GraphResult:
        return await tool_find_references(store, workspace, node_id, limit)

    @mcp.tool(
        description=(
            "Return nodes in OTHER languages that communicate with "
            "node_id via shared boundaries "
            "(HTTP routes, gRPC, queues). Shows cross-service connections."
        )
    )
    async def get_cross_language_calls(
        node_id: str, limit: Limit = 200
    ) -> GraphResult:
        return await tool_get_cross_language_calls(
            store, workspace, node_id, limit
        )

    @mcp.tool(
        description=(
            "Search file CONTENT by regex/text — the grep replacement. "
            "Use for string literals, log/error messages, comments, TODOs, "
            "and config values that symbol search cannot see. For 'where is "
            "X defined / who calls it', prefer search_symbols + get_callers."
        )
    )
    async def search_code(
        pattern: str,
        path_glob: str | None = None,
        ignore_case: bool = False,
        limit: Limit = 100,
    ) -> CodeSearchResult:
        return await tool_search_code(
            workspace,
            pattern,
            path_glob=path_glob,
            ignore_case=ignore_case,
            limit=limit,
        )

    @mcp.tool(
        description=(
            "Search the codebase by MEANING (natural language or code-like "
            "query) when you don't know the exact name. Each hit carries the "
            "graph node ids it overlaps, so you can pivot into "
            "get_callers/get_callees. Reports available=false if the "
            "[semantic] extra is not installed."
        )
    )
    async def search_semantic(query: str, limit: Limit = 10) -> SemanticResult:
        return await tool_search_semantic(store, workspace, query, limit)

    @mcp.tool(
        description=(
            "Find code semantically SIMILAR to a given symbol (by node_id). "
            "Returns resembling chunks bridged back to graph nodes — 'find "
            "other places that do something like this'. Requires the "
            "[semantic] extra."
        )
    )
    async def find_related(node_id: str, limit: Limit = 5) -> SemanticResult:
        return await tool_find_related(store, workspace, node_id, limit)

    @mcp.tool(
        description=(
            "List the codebase's semantic CLUSTERS — labeled zones of related "
            "symbols (auth, serialization, …). Use to orient in an unfamiliar "
            "repo, then get_cluster to drill in. Requires the [semantic] "
            "extra."
        )
    )
    async def list_clusters(
        min_size: int = 2, limit: Limit = 50
    ) -> ClusterList:
        return await tool_list_clusters(store, workspace, min_size, limit)

    @mcp.tool(
        description=(
            "Show the semantic cluster a symbol (node_id) belongs to and its "
            "sibling members — the semantic neighborhood around a symbol, "
            "complementing the structural get_neighbors. Requires the "
            "[semantic] extra."
        )
    )
    async def get_cluster(node_id: str, limit: Limit = 50) -> ClusterInfo:
        return await tool_get_cluster(store, workspace, node_id, limit)

    return mcp


def run_server(
    db_path: Path,
    project_root: Path,
    *,
    watch: bool = True,
) -> None:
    """
    Entry point for `graphlens-mcp serve`.

    When *watch* is true a filesystem watcher re-indexes files edited on
    disk even if no tool queries them, so the graph stays fresh on its own.
    """

    async def _main() -> None:
        workspace = await Workspace.create(project_root, db_path)
        mcp = create_mcp(workspace.store, workspace)
        try:
            # Catch up on files created/deleted/edited while the server was
            # down, then let the watcher keep the graph fresh from here on.
            # Inside the try so close() still runs if reconcile/watch fails.
            await workspace.reconcile()
            # Finish a semantic/cluster build that a prior run's crash left
            # unfinished (no-op when the last index completed cleanly).
            await workspace.resume_pending_index()
            if watch:
                workspace.start_watching()
            await mcp.run_stdio_async()
        finally:
            # Release the DB connection and shut down resolver/LSP
            # processes on exit.
            await workspace.close()

    asyncio.run(_main())
