"""
MCP tool implementations over SqliteStore + Workspace.

Each tool returns a typed Pydantic model (see
:mod:`graphlens_mcp.server.models`) so the agent gets a stable,
self-describing contract: every list response carries
``resolver_status`` (graph quality) and a ``truncated`` flag, and
lookups that touch a file trigger the on-access freshness check first.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from graphlens_mcp.server.models import (
    FileStructureResult,
    GraphResult,
    NodeInfoResult,
    NodeRef,
    to_refs,
)
from graphlens_mcp.store.sqlite_store import SqliteStore, worst_status

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from graphlens_mcp.indexer.workspace import Workspace

# Metadata keys graphlens adapters commonly attach to a definition node.
_SIGNATURE_KEYS = ("signature", "sig")
_DOCSTRING_KEYS = ("docstring", "doc", "documentation")


def _read_span(path: str | None, span_json: str | None) -> str | None:
    # Read the span directly from disk (1-based, inclusive line range).
    # We avoid linecache here: it caches file contents per-process and
    # would return STALE source after an edit, defeating the on-access
    # freshness guarantee.
    if not path or not span_json:
        return None
    try:
        start_line, _, end_line, _ = json.loads(span_json)
        with Path(path).open(encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        snippet = lines[start_line - 1 : end_line]
        return "".join(snippet).rstrip("\n")
    except (OSError, ValueError, IndexError):
        return None


def _first_meta(
    metadata_json: str | None, keys: tuple[str, ...]
) -> str | None:
    """Return the first present string value among *keys* in metadata."""
    if not metadata_json:
        return None
    try:
        meta = json.loads(metadata_json)
    except (ValueError, TypeError):
        return None
    if not isinstance(meta, dict):
        return None
    for key in keys:
        value = meta.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _resolve_in_project(workspace: Workspace, path: str) -> Path:
    """Resolve *path* against the project root, not the server cwd."""
    p = Path(path)
    if not p.is_absolute():
        p = workspace.project_root / p
    return p.resolve()


async def _fresh_status(workspace: Workspace, node: dict) -> str:
    file_path = node.get("file_path")
    if not file_path:
        return "ok"
    return await workspace.ensure_fresh(Path(file_path))


async def _aggregate_status(
    store: SqliteStore, base_status: str, rows: list[dict]
) -> str:
    """
    Fold *base_status* with the stored status of every returned file.

    The freshness check only refreshes the queried node's own file, so a
    walk can return callers/callees from files indexed at ``degraded``
    quality (a missing toolchain). Reporting only the queried node's status
    would let the agent treat such a partial answer as complete; instead we
    surface the worst status across all returned files.
    """
    paths = sorted({r["file_path"] for r in rows if r.get("file_path")})
    stored = await store.get_worst_status_for_files(paths) if paths else "ok"
    return worst_status(base_status, stored)


async def tool_search_symbols(
    store: SqliteStore,
    query: str,
    limit: int = 20,
) -> GraphResult:
    """
    Search for symbols by name across the whole codebase.

    Use this as the FIRST step when you need to find where a symbol is defined.
    Returns node IDs you can pass to get_callees / get_callers / get_node_info.
    The query supports FTS5 prefix syntax (e.g. ``create_order*``).
    """
    rows = await store.search_symbols(query, limit=limit)
    refs, truncated = to_refs(rows, limit)
    status = await _aggregate_status(
        store, "ok", [r.model_dump() for r in refs]
    )
    return GraphResult(
        nodes=refs,
        count=len(refs),
        resolver_status=status,
        truncated=truncated,
    )


async def tool_get_node_info(
    store: SqliteStore,
    workspace: Workspace,
    node_id: str,
) -> NodeInfoResult:
    """
    Return full info for a node: signature, docstring, source snippet.

    Triggers on-access freshness check so the source is up-to-date.
    ``signature`` and ``docstring`` are surfaced when the language
    adapter recorded them in node metadata; ``source`` is always read
    live from disk for the node's span.
    """
    node = await store.get_node(node_id)
    if node is None:
        return NodeInfoResult(error=f"Node {node_id!r} not found")

    status = await _fresh_status(workspace, node)
    node = await store.get_node(node_id) or node
    source = _read_span(node.get("file_path"), node.get("span_json"))
    return NodeInfoResult(
        node=NodeRef.from_row(node),
        source=source,
        signature=_first_meta(node.get("metadata_json"), _SIGNATURE_KEYS),
        docstring=_first_meta(node.get("metadata_json"), _DOCSTRING_KEYS),
        resolver_status=status,
    )


async def tool_get_file_structure(
    store: SqliteStore,
    workspace: Workspace,
    path: str,
    limit: int = 200,
) -> FileStructureResult:
    """
    Return the symbol outline of a file (classes, functions, methods).

    Triggers on-access freshness check. Use this instead of reading the whole
    file when you only need to understand its structure.
    """
    abs_path = str(_resolve_in_project(workspace, path))
    status = await workspace.ensure_fresh(Path(abs_path))
    rows = await store.get_nodes_in_file(abs_path)
    refs, truncated = to_refs(rows, limit)
    return FileStructureResult(
        path=abs_path, nodes=refs, resolver_status=status, truncated=truncated
    )


async def _walk_tool(
    store: SqliteStore,
    workspace: Workspace,
    node_id: str,
    query: Callable[[], Awaitable[list[dict[str, Any]]]],
    *,
    limit: int,
) -> GraphResult:
    node = await store.get_node(node_id)
    if node is None:
        return GraphResult(error=f"Node {node_id!r} not found")
    base = await _fresh_status(workspace, node)
    rows = await query()
    refs, truncated = to_refs(rows, limit)
    status = await _aggregate_status(store, base, rows)
    return GraphResult(
        nodes=refs,
        count=len(refs),
        resolver_status=status,
        truncated=truncated,
    )


async def tool_get_callees(
    store: SqliteStore,
    workspace: Workspace,
    node_id: str,
    max_depth: int = 3,
    limit: int = 200,
) -> GraphResult:
    """
    Return nodes node_id calls (outgoing CALLS edges, up to max_depth).

    Use this to understand what a function depends on internally.
    """
    depth = min(max_depth, 10)
    return await _walk_tool(
        store,
        workspace,
        node_id,
        lambda: store.get_callees(node_id, max_depth=depth),
        limit=limit,
    )


async def tool_get_callers(
    store: SqliteStore,
    workspace: Workspace,
    node_id: str,
    max_depth: int = 3,
    limit: int = 200,
) -> GraphResult:
    """
    Return nodes that call node_id (incoming CALLS edges, up to depth).

    PRIMARY tool for impact analysis: 'what breaks if I change X?'
    """
    depth = min(max_depth, 10)
    return await _walk_tool(
        store,
        workspace,
        node_id,
        lambda: store.get_callers(node_id, max_depth=depth),
        limit=limit,
    )


async def tool_get_neighbors(
    store: SqliteStore,
    workspace: Workspace,
    node_id: str,
    depth: int = 2,
    limit: int = 200,
) -> GraphResult:
    """
    Return nodes within depth hops of node_id in any direction (any edge type).

    Use this to explore what's nearby in the graph around an unknown symbol.
    """
    hops = min(depth, 5)
    return await _walk_tool(
        store,
        workspace,
        node_id,
        lambda: store.get_neighbors(node_id, depth=hops),
        limit=limit,
    )


async def tool_find_references(
    store: SqliteStore,
    workspace: Workspace,
    node_id: str,
    limit: int = 200,
) -> GraphResult:
    """
    Return nodes that reference node_id (non-call usages).

    Non-call usages include type annotations and assignments. Use
    alongside get_callers for a complete impact analysis.
    """
    return await _walk_tool(
        store,
        workspace,
        node_id,
        lambda: store.find_references(node_id),
        limit=limit,
    )


async def tool_get_cross_language_calls(
    store: SqliteStore,
    workspace: Workspace,
    node_id: str,
    limit: int = 200,
) -> GraphResult:
    """
    Return nodes in other languages that communicate with node_id.

    Finds cross-service connections via shared boundaries: HTTP routes,
    gRPC methods, queue topics. Works by tracing COMMUNICATES_WITH edges
    and shared BOUNDARY nodes (populated at init/reindex).
    """
    return await _walk_tool(
        store,
        workspace,
        node_id,
        lambda: store.get_cross_language_calls(node_id),
        limit=limit,
    )
