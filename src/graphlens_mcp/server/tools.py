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
import fnmatch
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from graphlens_mcp.indexer.semantic import (
    _DOCSTRING_KEYS,
    _SIGNATURE_KEYS,
    _first_meta,
)
from graphlens_mcp.server.models import (
    MAX_RESULTS,
    ClusterInfo,
    ClusterList,
    CodeMatch,
    CodeSearchResult,
    ExploreResult,
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

    from graphlens_mcp.indexer.workspace import Workspace

# Directories never worth grepping; ripgrep also honors .gitignore, this is the
# pure-Python fallback's equivalent of the watcher's _EXCLUDED_DIRS.
_GREP_EXCLUDED = frozenset(
    {".graphlens", ".git", "node_modules", ".venv", "venv", "__pycache__"}
)
# ripgrep's exit code for a fatal error (e.g. an invalid regex); exit 1 means
# "no matches" which is not an error.
_RG_ERROR_EXIT = 2


def _norm_path_glob(glob: str | None) -> str | None:
    """
    Normalize a path filter so agents can pass a bare directory.

    A value with no glob metacharacter (``* ? [``) is treated as a directory
    and expanded to ``<dir>/**`` — so ``"src/auth"`` scopes to everything under
    it. Values that already look like a glob (``"*.py"``, ``"src/**/*.go"``)
    pass through unchanged.
    """
    if not glob:
        return None
    g = glob.strip()
    if not g:
        return None
    if not any(c in g for c in "*?["):
        g = g.rstrip("/") + "/**"
    return g


def _path_matches(file_path: str | None, glob: str | None) -> bool:
    """Report whether *file_path* satisfies *glob* (dir-normalized)."""
    g = _norm_path_glob(glob)
    if g is None:
        return True
    if not file_path:
        return False
    if "/" not in g:
        return fnmatch.fnmatch(file_path.rsplit("/", 1)[-1], g)
    return fnmatch.fnmatch(file_path, g) or fnmatch.fnmatch(
        file_path, "*/" + g
    )


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
    path_glob: str | None = None,
) -> GraphResult:
    """
    Search for symbols by name across the whole codebase.

    Returns node IDs (also accepted by name) for get_node_info /
    get_callers / get_callees / explore. Supports FTS5 prefix syntax
    (``create_order*``). Scope to part of the tree with *path_glob* —
    a glob (``"*.py"``, ``"src/**"``) or a bare directory (``"src/auth"``).

    **Short or common names rank poorly** — dozens of imports and file
    nodes share them. Use the most distinctive form available: a
    compound name (``UserRepository``), a qualified path prefix
    (``models.Location``), or a wildcard suffix (``OrderSvc*``). When
    you know the file, ``get_file_structure`` is more reliable.

    Nodes returned with ``file_path: null`` are external stubs (not
    defined locally). Use ``get_file_structure`` on an importer to
    find the real definition.  When you don't know the name at all,
    use ``search_semantic`` instead.
    """
    # Over-fetch when filtering so the path scope doesn't starve the result.
    fetch = limit if path_glob is None else max(limit, MAX_RESULTS)
    rows = await store.search_symbols(query, limit=fetch)
    if path_glob is not None:
        rows = [
            r for r in rows if _path_matches(r.get("file_path"), path_glob)
        ]
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
    resolved = await _resolve_node_id(store, node_id)
    if resolved is None:
        return NodeInfoResult(
            error=_NO_NODE.format(ref=node_id), indexing=workspace.is_indexing
        )
    node = await store.get_node(resolved)
    if node is None:
        return NodeInfoResult(
            error=_NO_NODE.format(ref=node_id), indexing=workspace.is_indexing
        )
    node_id = resolved

    status = await _fresh_status(workspace, node)
    node = await store.get_node(node_id) or node
    source = _read_span(node.get("file_path"), node.get("span_json"))
    return NodeInfoResult(
        node=NodeRef.from_row(node),
        source=source,
        signature=_first_meta(node.get("metadata_json"), _SIGNATURE_KEYS),
        docstring=_first_meta(node.get("metadata_json"), _DOCSTRING_KEYS),
        resolver_status=status,
        indexing=workspace.is_indexing,
    )


async def tool_get_file_structure(
    store: SqliteStore,
    workspace: Workspace,
    path: str,
    limit: int = 200,
) -> FileStructureResult:
    """
    Return the symbol outline of a single file (classes, functions, methods).

    *path* must be a **file** path — passing a directory returns empty.
    For directory-level exploration pass a qualified name prefix to
    ``search_symbols`` (e.g. ``"authz."``), or use ``search_semantic``
    for concept-level discovery.

    Triggers on-access freshness check.  Prefer this over reading the
    whole file when you only need its structure or want to reliably
    get a node ID for a common name.  Nodes with ``file_path: null``
    are external symbols resolved here but not defined locally.
    """
    abs_path = str(_resolve_in_project(workspace, path))
    status = await workspace.ensure_fresh(Path(abs_path))
    rows = await store.get_nodes_in_file(abs_path)
    refs, truncated = to_refs(rows, limit)
    return FileStructureResult(
        path=abs_path,
        nodes=refs,
        resolver_status=status,
        truncated=truncated,
        indexing=workspace.is_indexing,
    )


# Steering error: tells the agent how to recover instead of just failing.
_NO_NODE = (
    "No node or symbol matches {ref!r}. Pass a node id from search_symbols "
    "or explore, or a distinctive symbol name (a compound or qualified name)."
)


def _pick_node(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Prefer a locally-defined node (file_path set) over an external stub."""
    return next((r for r in rows if r.get("file_path")), rows[0])


async def _resolve_node_id(store: SqliteStore, ref: str) -> str | None:
    """
    Resolve *ref* — a node id OR a symbol name — to a node id.

    Tools accept either so the agent can call e.g.
    ``get_callers("create_order")`` directly without a separate
    search_symbols round-trip. Resolution order: exact node id, then an
    exact-case name match (so ``ResponseWriter`` picks the interface, not the
    FTS-adjacent ``responseWriter`` struct), then the best FTS match. Returns
    None if nothing matches.
    """
    if not ref:
        return None
    if await store.get_node(ref) is not None:
        return ref
    exact = await store.find_nodes_by_exact_name(ref)
    if exact:
        return _pick_node(exact)["id"]
    rows = await store.search_symbols(ref, limit=1)
    return rows[0]["id"] if rows else None


async def _walk_tool(
    store: SqliteStore,
    workspace: Workspace,
    ref: str,
    query: Callable[[str], Awaitable[list[dict[str, Any]]]],
    *,
    limit: int,
) -> GraphResult:
    node_id = await _resolve_node_id(store, ref)
    if node_id is None:
        return GraphResult(
            error=_NO_NODE.format(ref=ref), indexing=workspace.is_indexing
        )
    node = await store.get_node(node_id)
    if node is None:  # resolved id vanished between resolve and fetch (a race)
        return GraphResult(
            error=_NO_NODE.format(ref=ref), indexing=workspace.is_indexing
        )
    base = await _fresh_status(workspace, node)
    rows = await query(node_id)
    refs, truncated = to_refs(rows, limit)
    status = await _aggregate_status(store, base, rows)
    return GraphResult(
        nodes=refs,
        count=len(refs),
        resolver_status=status,
        truncated=truncated,
        indexing=workspace.is_indexing,
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
        lambda nid: store.get_callees(nid, max_depth=depth),
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
        lambda nid: store.get_callers(nid, max_depth=depth),
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
        lambda nid: store.get_neighbors(nid, depth=hops),
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
        store.find_references,
        limit=limit,
    )


async def tool_get_implementors(
    store: SqliteStore,
    workspace: Workspace,
    node_id: str,
    max_depth: int = 5,
    limit: int = 200,
) -> GraphResult:
    """
    Return nodes that inherit from / implement node_id (subtypes).

    The reverse of inheritance: subclasses, interface implementors and
    embedders. THE tool for "what implements / extends / subclasses X?".
    """
    depth = min(max_depth, 10)
    return await _walk_tool(
        store,
        workspace,
        node_id,
        lambda nid: store.get_implementors(nid, max_depth=depth),
        limit=limit,
    )


async def tool_explore(
    store: SqliteStore,
    workspace: Workspace,
    query: str,
    limit: int = 20,
) -> ExploreResult:
    """
    Resolve a symbol and return its definition plus immediate relations.

    One call answers "what is X, who uses it, what implements it": it
    searches for the symbol, reads the best match's source/signature, and
    bundles its direct callers, callees, implementors and references — so the
    agent rarely needs to chain search_symbols -> get_node_info -> get_callers.
    """
    rows = await store.search_symbols(query, limit=limit)
    if not rows:
        return ExploreResult(
            error=(
                f"No symbol matches {query!r}. Try a shorter or partial name, "
                "or search_code to grep for the term in source."
            ),
            indexing=workspace.is_indexing,
        )
    top = rows[0]
    node_id = top["id"]
    node = await store.get_node(node_id) or top
    base = await _fresh_status(workspace, node)
    node = await store.get_node(node_id) or node

    # One shallow hop of each relation; small caps keep the response lean.
    rel_cap = min(limit, 25)
    callers = await store.get_callers(node_id, max_depth=1)
    callees = await store.get_callees(node_id, max_depth=1)
    implementors = await store.get_implementors(node_id, max_depth=2)
    references = await store.find_references(node_id)

    caller_refs, t1 = to_refs(callers, rel_cap)
    callee_refs, t2 = to_refs(callees, rel_cap)
    impl_refs, t3 = to_refs(implementors, rel_cap)
    ref_refs, t4 = to_refs(references, rel_cap)
    cand_refs, _ = to_refs(rows[1:], 10)

    status = await _aggregate_status(
        store, base, callers + callees + implementors + references
    )
    return ExploreResult(
        node=NodeRef.from_row(node),
        source=_read_span(node.get("file_path"), node.get("span_json")),
        signature=_first_meta(node.get("metadata_json"), _SIGNATURE_KEYS),
        docstring=_first_meta(node.get("metadata_json"), _DOCSTRING_KEYS),
        callers=caller_refs,
        callees=callee_refs,
        implementors=impl_refs,
        references=ref_refs,
        candidates=cand_refs,
        resolver_status=status,
        truncated=t1 or t2 or t3 or t4,
        indexing=workspace.is_indexing,
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
        store.get_cross_language_calls,
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
    r"""
    Search file *content* by regex — the in-graph replacement for grep.

    *pattern* is a PCRE regular expression (ripgrep).  Escape regex
    metacharacters for literal searches — parentheses, brackets, dots,
    ``*``, ``+`` must be escaped: ``foo\\(bar\\)``, ``os\\.path``.
    Scope with *path_glob*: a glob like ``"*.py"`` or ``"src/**/*.go"``,
    or a **bare directory** like ``"src/auth"`` (expanded to ``src/auth/**``).

    Use for: string literals, log/error messages, comments, TODOs,
    config values, or raw-text patterns the symbol graph cannot answer.
    For symbol-level questions ("where is X defined / who calls it?")
    prefer ``search_symbols`` + ``get_callers`` or ``explore`` — they are
    precise and name-resolved.  Honors .gitignore; skips vendored dirs.
    """
    glob = _norm_path_glob(path_glob)
    cap = min(limit, MAX_RESULTS)
    root = workspace.project_root
    try:
        matches, truncated = await _ripgrep(
            root,
            pattern,
            path_glob=glob,
            ignore_case=ignore_case,
            cap=cap,
        )
    except FileNotFoundError:
        # No ripgrep binary — fall back to a pure-Python scan.
        try:
            matches, truncated = await asyncio.to_thread(
                _python_grep, root, pattern, glob, ignore_case, cap
            )
        except _GrepError as exc:
            return CodeSearchResult(
                error=str(exc), indexing=workspace.is_indexing
            )
    except _GrepError as exc:
        return CodeSearchResult(error=str(exc), indexing=workspace.is_indexing)
    return CodeSearchResult(
        matches=matches,
        count=len(matches),
        truncated=truncated,
        indexing=workspace.is_indexing,
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
# Semantic search / find_related (bundled model2vec embeddings)
# ----------------------------------------------------------------------


def _hit_to_model(h: Any) -> SemanticHit:
    """Convert an indexer SemanticHit dataclass to the Pydantic model."""
    return SemanticHit(
        node_id=h.node_id,
        kind=h.kind,
        name=h.name,
        qualified_name=h.qualified_name,
        file_path=h.file_path,
        score=h.score,
    )


async def tool_search_semantic(
    store: SqliteStore,
    workspace: Workspace,
    query: str,
    limit: int = 10,
) -> SemanticResult:
    """
    Search the codebase by *meaning* — the primary discovery tool.

    Use when you don't know the symbol name: describe the behavior or
    concept in natural language ("retry HTTP with backoff", "parse JWT
    token", "validate user permissions"). Also effective for concept
    names without a specific identifier ("authentication", "rate limit").

    Each hit is a graph node — pass ``node_id`` directly to
    ``get_callers`` / ``get_callees`` / ``get_node_info``.  When
    ``available=False`` in the response, fall back to ``search_symbols``
    + ``search_code``.
    """
    cap = min(limit, MAX_RESULTS)
    response = await workspace.semantic.search(store, query, top_k=cap)
    if not response.available:
        return SemanticResult(available=False, reason=response.reason)
    hits = [_hit_to_model(h) for h in response.hits]
    return SemanticResult(hits=hits, count=len(hits), available=True)


async def tool_find_related(
    store: SqliteStore,
    workspace: Workspace,
    node_id: str,
    limit: int = 5,
) -> SemanticResult:
    """
    Find graph nodes semantically similar to a given symbol.

    Pass a node id (from search_symbols / search_semantic); returns graph
    nodes whose embedding is closest to the source node — "find other code
    that does something like this".
    """
    node = await store.get_node(node_id)
    if node is None:
        return SemanticResult(error=f"Node {node_id!r} not found")
    cap = min(limit, MAX_RESULTS)
    response = await workspace.semantic.find_related(store, node_id, top_k=cap)
    if not response.available:
        return SemanticResult(available=False, reason=response.reason)
    hits = [_hit_to_model(h) for h in response.hits]
    return SemanticResult(hits=hits, count=len(hits), available=True)


# ----------------------------------------------------------------------
# Semantic clusters
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
    get_cluster to drill into one.
    """
    if not await workspace.ensure_clusters():
        reason = workspace.semantic.availability.reason
        return ClusterList(
            available=False,
            reason=reason or "Clusters could not be computed.",
        )
    cap = min(limit, MAX_RESULTS)
    rows = await store.list_clusters(min_size=min_size, limit=cap + 1)
    truncated = len(rows) > cap
    refs = [cluster_ref_from_row(r) for r in rows[:cap]]
    return ClusterList(
        clusters=refs,
        count=len(refs),
        available=True,
        truncated=truncated,
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
    the structural get_neighbors.
    """
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
