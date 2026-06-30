"""FastMCP server: graph navigation + optional semantic search/clusters."""

from __future__ import annotations

import asyncio
import contextlib
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
    ExploreResult,
    FileStructureResult,
    GraphResult,
    NodeInfoResult,
    SemanticResult,
)
from graphlens_mcp.server.tools import (
    tool_explore,
    tool_find_references,
    tool_find_related,
    tool_get_callees,
    tool_get_callers,
    tool_get_cluster,
    tool_get_cross_language_calls,
    tool_get_file_structure,
    tool_get_implementors,
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
# Depth bounds are intentionally NOT upper-capped here: a too-large value is
# clamped by the tool (to 10 / 5) rather than rejected, so a slightly-off
# argument never costs the agent a turn.
# Like Depth, Limit is not upper-capped here: a too-large value is clamped to
# MAX_RESULTS (200) by the tool, not rejected, so it never costs a turn.
Limit = Annotated[
    int, Field(ge=1, description="Max nodes to return (clamped to 200)")
]
Depth = Annotated[
    int, Field(ge=1, description="Max traversal hops (clamped to 10)")
]
NeighborDepth = Annotated[
    int, Field(ge=1, description="Max neighbor hops (clamped to 5)")
]


def create_mcp(store: SqliteStore, workspace: Workspace) -> FastMCP:
    """Build the FastMCP server exposing the graph navigation tools."""
    mcp = FastMCP(
        "graphlens",
        instructions=(
            "Semantic code graph for this project — prefer over grep/file "
            "reads. explore(name) returns a symbol's source plus its callers, "
            "callees, implementors and references in one call. Relation tools "
            "(get_callers/callees/implementors/node_info) take a node id OR a "
            "name. search_semantic finds by meaning; search_code greps raw "
            "text. Responses carry resolver_status (degraded = approximate) "
            "and indexing (true = reindex running, edges may be incomplete — "
            "don't call a symbol unused yet)."
        ),
    )

    @mcp.tool(
        description=(
            "Find symbols by name (FTS, prefix syntax 'foo*'); returns node "
            "ids. Scope with path_glob ('*.py', 'src/auth'). Often skippable: "
            "relation tools take a name directly, and explore returns a "
            "symbol with its relations in one call."
        )
    )
    async def search_symbols(
        query: str, limit: Limit = 20, path_glob: str | None = None
    ) -> GraphResult:
        return await tool_search_symbols(store, query, limit, path_glob)

    @mcp.tool(
        description=(
            "One call for a symbol: source + signature plus its direct "
            "callers, callees, implementors and references. Prefer over "
            "chaining search_symbols/get_node_info/get_callers or grepping."
        )
    )
    async def explore(query: str, limit: Limit = 20) -> ExploreResult:
        return await tool_explore(store, workspace, query, limit)

    @mcp.tool(
        description=(
            "A symbol's source, signature, kind and location. Accepts a node "
            "id OR a name."
        )
    )
    async def get_node_info(node_id: str) -> NodeInfoResult:
        return await tool_get_node_info(store, workspace, node_id)

    @mcp.tool(
        description=(
            "Symbol outline of a file (classes, functions, methods). Use "
            "instead of reading the whole file for structure."
        )
    )
    async def get_file_structure(
        path: str, limit: Limit = 200
    ) -> FileStructureResult:
        return await tool_get_file_structure(store, workspace, path, limit)

    @mcp.tool(
        description=(
            "What a symbol CALLS (outgoing, up to max_depth hops). Accepts a "
            "node id OR a name."
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
            "What CALLS a symbol (incoming, up to max_depth hops). Accepts a "
            "node id OR a name. Primary impact-analysis tool ('what breaks if "
            "I change X?'); don't grep for call sites."
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
            "Nodes within depth hops, any direction. Accepts a node id OR a "
            "name (explore is usually the better first call)."
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
            "Non-call references to a symbol (type annotations, assignments). "
            "Accepts a node id OR a name. For subtypes use get_implementors."
        )
    )
    async def find_references(node_id: str, limit: Limit = 200) -> GraphResult:
        return await tool_find_references(store, workspace, node_id, limit)

    @mcp.tool(
        description=(
            "Subclasses / interface implementors / embedders of a symbol. "
            "Accepts a node id OR a name. First choice for 'what implements / "
            "extends / subclasses X?'. If it returns nothing, the link may be "
            "unresolved (composition, a foreign toolchain) — fall back to "
            "search_code or get_file_structure rather than looping."
        )
    )
    async def get_implementors(
        node_id: str, max_depth: Depth = 5, limit: Limit = 200
    ) -> GraphResult:
        return await tool_get_implementors(
            store, workspace, node_id, max_depth, limit
        )

    @mcp.tool(
        description=(
            "Cross-language callers via shared boundaries (HTTP routes, gRPC, "
            "queues). Accepts a node id OR a name."
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
            "Regex/text content search — the grep replacement for string "
            "literals, logs, comments and config. For 'where is X / who calls "
            "it' prefer explore or search_symbols. Scope with path_glob "
            "('*.py', 'src/**/*.go', or a bare directory 'src/auth')."
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
            "Search by MEANING when you don't know the name. Hits carry node "
            "ids to pivot into the graph. available=false if the embedding "
            "model can't be fetched (offline)."
        )
    )
    async def search_semantic(query: str, limit: Limit = 10) -> SemanticResult:
        return await tool_search_semantic(store, workspace, query, limit)

    @mcp.tool(
        description=(
            "Code semantically similar to a symbol (node_id) — 'other places "
            "that do something like this'. available=false if the embedding "
            "model can't be fetched (offline)."
        )
    )
    async def find_related(node_id: str, limit: Limit = 5) -> SemanticResult:
        return await tool_find_related(store, workspace, node_id, limit)

    @mcp.tool(
        description=(
            "Labeled semantic clusters — orient in an unfamiliar repo, then "
            "get_cluster to drill in. available=false if the embedding model "
            "can't be fetched (offline)."
        )
    )
    async def list_clusters(
        min_size: int = 2, limit: Limit = 50
    ) -> ClusterList:
        return await tool_list_clusters(store, workspace, min_size, limit)

    @mcp.tool(
        description=(
            "The semantic cluster a symbol (node_id) belongs to and its "
            "siblings. available=false if the model can't be fetched."
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

    async def _catch_up(workspace: Workspace) -> None:
        # Reconcile files changed while the server was down, then finish any
        # semantic/cluster build a prior crash left pending. Runs in the
        # background so the server answers tool calls immediately instead of
        # blocking startup; while it runs tools report ``indexing=true``.
        try:
            await workspace.reconcile()
            await workspace.resume_pending_index()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("background reconcile/resume failed")

    async def _main() -> None:
        workspace = await Workspace.create(project_root, db_path)
        mcp = create_mcp(workspace.store, workspace)
        # Start serving right away; catch-up indexing proceeds concurrently.
        catch_up = asyncio.create_task(_catch_up(workspace))
        try:
            if watch:
                workspace.start_watching()
            await mcp.run_stdio_async()
        finally:
            catch_up.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await catch_up
            # Release the DB connection and shut down resolver/LSP
            # processes on exit.
            await workspace.close()

    asyncio.run(_main())
