"""FastMCP server: graph navigation + optional semantic search/clusters."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Annotated

from fastmcp import FastMCP
from pydantic import Field

from graphlens_mcp.indexer.workspace import Workspace
from graphlens_mcp.server.lean import tool_info, tool_relations, tool_search

# Runtime import (NOT under TYPE_CHECKING): with `from __future__ import
# annotations` every annotation is a string, and FastMCP evaluates each tool's
# return annotation at registration time to build its output schema. Under
# TYPE_CHECKING that eval raises NameError and the server fails to start, so
# the agent reports it "cannot connect". noqa: TC001 stops ruff re-hiding it.
from graphlens_mcp.server.models import (  # noqa: TC001
    InfoResult,
    RelationsResult,
    SearchResult,
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


def create_mcp(store: SqliteStore, workspace: Workspace) -> FastMCP:
    """
    Build the FastMCP server: three tools, everything is a graph node.

    ``search`` finds nodes by name, content, or meaning; ``relations`` gives a
    symbol's callers/callees/implementors/references; ``info`` reads a
    symbol's source+signature or a file's outline. Chain them: search -> pick
    a node -> relations/info.
    """
    mcp = FastMCP(
        "graphlens",
        instructions=(
            "Semantic code graph for this project — prefer over grep/file "
            "reads. Three tools, everything is a node: search(query) finds "
            "nodes by name, content or meaning; relations(symbol) gives its "
            "callers/callees/implementors/references; info(target) reads a "
            "symbol's source+signature or a file's outline. Chain them: "
            "search -> pick a node -> relations/info — most questions answer "
            "in 1-3 calls. Trust the results; don't re-verify with grep — "
            "they come from a real parse, not text matching, and "
            "re-checking wastes context. Responses carry resolver_status "
            "(degraded=approximate) and indexing (true=reindex running, "
            "edges may lag). search excludes test files by default; pass "
            "path_glob to include them on purpose."
        ),
    )

    @mcp.tool(
        description=(
            "Find code by NAME, CONTENT, or MEANING — the one way in "
            "(default limit 25). Returns graph nodes WITH their signatures "
            "(not dead text lines): often enough to answer without info(). "
            "Pass any node to relations/"
            "info. Use wherever you'd grep or search for a symbol; content "
            "hits fold into the enclosing symbol. text_matches = non-symbol "
            "hits. Content is matched LITERALLY (not regex) — write "
            "'Request(' or 'getErrorMap(' as-is, no escaping needed. Scope "
            "with path_glob (e.g. 'tests/*', '*.ts', '!tests/*' to exclude) "
            "— there is no file:/content: query syntax, so use this instead "
            "of guessing one. Test files are excluded by default unless you "
            "set path_glob yourself or the query says 'test' — pass "
            "path_glob='tests/*' to search them on purpose. For 'list EVERY "
            "file that calls/imports X' — where the top-N node list could "
            "miss some — set exhaustive=true: returns every matching file "
            "path (no signatures), uncapped by the normal limit. If the "
            "response's note field is set, your exact text matched nothing "
            "— every node is a name/meaning guess, not a confirmed hit — "
            "simplify the query instead of repeating it."
        )
    )
    async def search(
        query: str,
        limit: Limit = 25,
        path_glob: str | None = None,
        exhaustive: bool = False,
    ) -> SearchResult:
        return await tool_search(
            store, workspace, query, limit, path_glob, exhaustive
        )

    @mcp.tool(
        description=(
            "A symbol's neighbourhood in one call: who calls it, what it "
            "calls, what implements/subclasses it, and non-call references — "
            "each with its signature. Accepts a node id OR a name (default "
            "depth 2, limit 25). THE tool for impact analysis and 'what "
            "implements X'. If the name matches several definitions, pass "
            "file (a path or suffix) to pin the right one — e.g. one "
            "UserService per service in a monorepo."
        )
    )
    async def relations(
        symbol: str,
        depth: Depth = 2,
        limit: Limit = 25,
        file: str | None = None,
    ) -> RelationsResult:
        return await tool_relations(
            store, workspace, symbol, depth, limit, file
        )

    @mcp.tool(
        description=(
            "Read a specific target. A SYMBOL (node id or name) -> source, "
            "signature and location. A FILE path -> its symbol outline by "
            "default (default limit 200 symbols) — a cheap structural "
            "overview. Set mode='source' to read the file's actual current "
            "content instead: line-numbered (same shape as Read, safe to "
            "Edit from), windowable with offset/limit exactly like Read, "
            "plus which files import it. Use mode='source' instead of "
            "opening the file yourself whenever you need the body, not "
            "just its symbol list. If a symbol name matches several "
            "definitions, pass file (a path or suffix) to pin the right "
            "one — e.g. one UserService per service in a monorepo."
        )
    )
    async def info(
        target: str,
        limit: Limit = 200,
        file: str | None = None,
        mode: str = "outline",
        offset: int = 1,
    ) -> InfoResult:
        return await tool_info(
            store, workspace, target, limit, file, mode, offset
        )

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
