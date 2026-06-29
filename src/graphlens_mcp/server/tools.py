"""
MCP tool implementations over SqliteStore + Workspace.

Each tool returns a typed Pydantic model (see
:mod:`graphlens_mcp.server.models`) so the agent gets a stable,
self-describing contract: every list response carries
``resolver_status`` (graph quality) and a ``truncated`` flag, and
lookups that touch a file trigger the on-access freshness check first.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from graphlens_mcp.indexer.semantic import semantic_availability
from graphlens_mcp.server.models import (
    MAX_RESULTS,
    ClusterInfo,
    ClusterList,
    CodeMatch,
    CodeSearchResult,
    FileStructureResult,
    GraphResult,
    NodeInfoResult,
    NodeRef,
    SemanticHit,
    SemanticResult,
    cluster_ref_from_row,
    to_refs,
)
from graphlens_mcp.store.sqlite_store import SqliteStore, worst_status

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from graphlens_mcp.indexer.semantic import SemanticResponse
    from graphlens_mcp.indexer.workspace import Workspace

# Directories never worth grepping; ripgrep also honors .gitignore, this is the
# pure-Python fallback's equivalent of the watcher's _EXCLUDED_DIRS.
_GREP_EXCLUDED = frozenset(
    {".graphlens", ".git", "node_modules", ".venv", "venv", "__pycache__"}
)
# ripgrep's exit code for a fatal error (e.g. an invalid regex); exit 1 means
# "no matches" which is not an error.
_RG_ERROR_EXIT = 2

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
    # Aggregate over the full result set (pre-cap) so a degraded file truncated
    # out of the response still lowers the reported status.
    status = await _aggregate_status(store, "ok", rows)
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


# ----------------------------------------------------------------------
# Content search (grep replacement) — no semantic dependency
# ----------------------------------------------------------------------


class _GrepError(Exception):
    """A search engine error (e.g. an invalid regular expression)."""


async def tool_search_code(
    workspace: Workspace,
    pattern: str,
    *,
    path_glob: str | None = None,
    ignore_case: bool = False,
    limit: int = 100,
) -> CodeSearchResult:
    """
    Search file *content* by regex/text — the in-graph replacement for grep.

    Use this for what the graph cannot answer from symbol structure: string
    literals, log/error messages, comments, TODOs, config values, or any
    raw-text pattern. For "where is symbol X defined / who calls it", prefer
    search_symbols + get_callers (precise, name-resolved). Honors .gitignore
    and skips vendored/build dirs.
    """
    cap = min(limit, MAX_RESULTS)
    root = workspace.project_root
    try:
        matches, truncated = await _ripgrep(
            root,
            pattern,
            path_glob=path_glob,
            ignore_case=ignore_case,
            cap=cap,
        )
    except FileNotFoundError:
        # No ripgrep binary — fall back to a pure-Python scan.
        try:
            matches, truncated = await asyncio.to_thread(
                _python_grep, root, pattern, path_glob, ignore_case, cap
            )
        except _GrepError as exc:
            return CodeSearchResult(error=str(exc))
    except _GrepError as exc:
        return CodeSearchResult(error=str(exc))
    return CodeSearchResult(
        matches=matches, count=len(matches), truncated=truncated
    )


async def _ripgrep(
    root: Path,
    pattern: str,
    *,
    path_glob: str | None,
    ignore_case: bool,
    cap: int,
) -> tuple[list[CodeMatch], bool]:
    """Run ripgrep with JSON output, capped at *cap* matches."""
    args = ["rg", "--json", "--no-messages"]
    if ignore_case:
        args.append("-i")
    args += ["--glob", "!.graphlens", "--glob", "!.git"]
    if path_glob:
        args += ["--glob", path_glob]
    args += ["-e", pattern, str(root)]

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    matches: list[CodeMatch] = []
    truncated = False
    killed = False
    if proc.stdout is None:  # pragma: no cover - PIPE always yields a stream
        await proc.wait()
        return matches, truncated
    async for raw in proc.stdout:
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if obj.get("type") != "match":
            continue
        data = obj["data"]
        text = data.get("lines", {}).get("text", "")
        matches.append(
            CodeMatch(
                file_path=data["path"]["text"],
                line=data["line_number"],
                text=text.rstrip("\n"),
            )
        )
        if len(matches) >= cap:
            truncated = True
            killed = True
            proc.kill()
            break

    stderr_data = b""
    if proc.stderr is not None:
        stderr_data = await proc.stderr.read()
    await proc.wait()
    # rg exits 2 on a fatal error (e.g. a bad regex); 1 == no matches is fine.
    if not killed and proc.returncode == _RG_ERROR_EXIT and not matches:
        msg = stderr_data.decode("utf-8", "replace").strip() or "search error"
        raise _GrepError(msg)
    return matches, truncated


def _python_grep(
    root: Path,
    pattern: str,
    path_glob: str | None,
    ignore_case: bool,
    cap: int,
) -> tuple[list[CodeMatch], bool]:
    """Pure-Python content search fallback when ripgrep is unavailable."""
    try:
        rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        msg = f"invalid pattern: {exc}"
        raise _GrepError(msg) from exc

    matches: list[CodeMatch] = []
    for path in root.rglob(path_glob or "*"):
        if not path.is_file():
            continue
        if _GREP_EXCLUDED & set(path.relative_to(root).parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if rx.search(line):
                matches.append(
                    CodeMatch(
                        file_path=str(path), line=lineno, text=line.rstrip()
                    )
                )
                if len(matches) >= cap:
                    return matches, True
    return matches, False


# ----------------------------------------------------------------------
# Semantic search / find_related (optional [semantic] extra)
# ----------------------------------------------------------------------


async def _bridge_hits(
    store: SqliteStore,
    workspace: Workspace,
    response: SemanticResponse,
) -> list[SemanticHit]:
    """Attach overlapping graph node refs to each semantic hit."""
    hits: list[SemanticHit] = []
    for hit in response.hits:
        abs_path = str(_resolve_in_project(workspace, hit.file_path))
        rows = await store.nodes_overlapping(
            abs_path, hit.start_line, hit.end_line, limit=3
        )
        hits.append(
            SemanticHit(
                file_path=abs_path,
                start_line=hit.start_line,
                end_line=hit.end_line,
                score=hit.score,
                content=hit.content,
                language=hit.language,
                nodes=[NodeRef.from_row(r) for r in rows],
            )
        )
    return hits


async def tool_search_semantic(
    store: SqliteStore,
    workspace: Workspace,
    query: str,
    limit: int = 10,
    max_snippet_lines: int = 40,
) -> SemanticResult:
    """
    Search the codebase by *meaning*, not just name or text.

    Best when you don't know the exact symbol name: a natural-language
    description ("retry an HTTP request with backoff") or a code-like query.
    Each hit carries the graph nodes it overlaps, so you can pivot straight
    into get_callers / get_callees. Requires the [semantic] extra; if it is
    unavailable the result says so (available=False) — fall back to
    search_symbols / search_code.
    """
    cap = min(limit, MAX_RESULTS)
    response = await workspace.semantic.search(
        query, top_k=cap, max_snippet_lines=max_snippet_lines
    )
    if not response.available:
        return SemanticResult(available=False, reason=response.reason)
    hits = await _bridge_hits(store, workspace, response)
    return SemanticResult(hits=hits, count=len(hits), available=True)


async def tool_find_related(
    store: SqliteStore,
    workspace: Workspace,
    node_id: str,
    limit: int = 5,
    max_snippet_lines: int = 40,
) -> SemanticResult:
    """
    Find code semantically similar to a given symbol.

    Pass a node id (from search_symbols / search_semantic); returns chunks
    that resemble that symbol's implementation, each bridged back to graph
    nodes. Useful for "find other places that do something like this".
    Requires the [semantic] extra.
    """
    node = await store.get_node(node_id)
    if node is None:
        return SemanticResult(error=f"Node {node_id!r} not found")
    await _fresh_status(workspace, node)
    node = await store.get_node(node_id) or node
    file_path = node.get("file_path")
    span_json = node.get("span_json")
    if not file_path or not span_json:
        return SemanticResult(
            error="Node has no source span to find related code from"
        )
    try:
        start_line, _, end_line, _ = json.loads(span_json)
    except (ValueError, TypeError):
        return SemanticResult(error="Node span is malformed")
    content = _read_span(file_path, span_json) or ""
    cap = min(limit, MAX_RESULTS)
    response = await workspace.semantic.find_related(
        file_path=str(_resolve_in_project(workspace, file_path)),
        start_line=start_line,
        end_line=end_line,
        content=content,
        language=None,
        top_k=cap,
        max_snippet_lines=max_snippet_lines,
    )
    if not response.available:
        return SemanticResult(available=False, reason=response.reason)
    hits = await _bridge_hits(store, workspace, response)
    return SemanticResult(hits=hits, count=len(hits), available=True)


# ----------------------------------------------------------------------
# Semantic clusters (optional [semantic] extra)
# ----------------------------------------------------------------------


async def tool_list_clusters(
    store: SqliteStore,
    workspace: Workspace,
    min_size: int = 2,
    limit: int = 50,
) -> ClusterList:
    """
    List the codebase's semantic clusters — labeled zones of related symbols.

    A map of "what this codebase is about": each cluster groups symbols that
    are semantically similar (e.g. auth, serialization, retry logic) with an
    auto-derived label. Use it to orient in an unfamiliar repo, then
    get_cluster to drill into one. Requires the [semantic] extra.
    """
    avail = semantic_availability()
    if not avail.ok:
        return ClusterList(available=False, reason=avail.reason)
    if not await workspace.ensure_clusters():
        reason = workspace.semantic.availability.reason
        return ClusterList(
            available=False,
            reason=reason or "Clusters could not be computed.",
        )
    cap = min(limit, MAX_RESULTS)
    rows = await store.list_clusters(min_size=min_size, limit=cap)
    refs = [cluster_ref_from_row(r) for r in rows]
    return ClusterList(
        clusters=refs,
        count=len(refs),
        available=True,
        truncated=len(rows) >= cap,
    )


async def tool_get_cluster(
    store: SqliteStore,
    workspace: Workspace,
    node_id: str,
    limit: int = 50,
) -> ClusterInfo:
    """
    Show the semantic cluster a symbol belongs to, and its sibling members.

    Pass a node id; returns its cluster (label + terms) and the other symbols
    grouped with it — the semantic neighborhood around a symbol, complementing
    the structural get_neighbors. Requires the [semantic] extra.
    """
    avail = semantic_availability()
    if not avail.ok:
        return ClusterInfo(available=False, reason=avail.reason)
    await workspace.ensure_clusters()
    cluster_id = await store.get_cluster_id_for_node(node_id)
    if cluster_id is None:
        if await store.get_node(node_id) is None:
            return ClusterInfo(error=f"Node {node_id!r} not found")
        return ClusterInfo(
            available=True,
            error="Node is not assigned to a cluster (too sparse to group).",
        )
    crow = await store.get_cluster(cluster_id)
    if crow is None:
        return ClusterInfo(available=True, error="Cluster no longer exists.")
    cap = min(limit, MAX_RESULTS)
    members = await store.get_cluster_members(cluster_id, limit=cap)
    return ClusterInfo(
        cluster=cluster_ref_from_row(crow),
        members=[NodeRef.from_row(m) for m in members],
        available=True,
        truncated=len(members) >= cap,
    )
