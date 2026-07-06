"""
The lean 3-handle surface: ``search`` -> ``relations`` -> ``info``.

Agents really use only "find something", "see what connects to it", "read
it" — and a search that returns navigable graph NODES (not dead grep lines)
kills the grep-grinding loop at the root. Everything here reuses the
existing store + tool helpers; nothing new in the engine.
"""

from __future__ import annotations

import fnmatch
import json
from pathlib import Path
from typing import TYPE_CHECKING

from graphlens_mcp.indexer.semantic import (
    _DOCSTRING_KEYS,
    _SIGNATURE_KEYS,
    _first_meta,
)
from graphlens_mcp.server.models import (
    InfoResult,
    NodeRef,
    RelationsResult,
    SearchResult,
    SigNode,
    TextMatch,
    to_file_nodes,
)
from graphlens_mcp.server.tools import (
    _aggregate_status,
    _fresh_status,
    _GrepError,
    _read_span,
    _resolve_in_project,
    _resolve_node_id,
    _ripgrep,
    _ripgrep_files_only,
)

if TYPE_CHECKING:
    from graphlens_mcp.indexer.workspace import Workspace
    from graphlens_mcp.store.sqlite_store import SqliteStore

# Hard caps on how much a single tool response can hand back. The agent can
# pass a larger `limit`, but these clamp it — a response of tens of thousands
# chars is re-sent to the model every turn (quadratic context growth) and the
# single biggest token sink we measured. Kept well under the host's ~25K inline
# result ceiling so nothing gets externalized + re-Read.
_SEARCH_CAP = 20  # max nodes from one search
_REL_CAP = 40  # max nodes per relation list (x4); char ceiling backstops size
_FILE_OUTLINE_CAP = 60  # max symbols from info(file)
_TEXT_MATCH_CAP = 8
_MAX_TEXT_LEN = 200
_MAX_RESPONSE_CHARS = 18000  # belt-and-suspenders ceiling on a search response
# exhaustive=True returns bare file paths (no signature/text per hit), so a
# much higher cap is still cheap — this is specifically for "every file that
# calls/imports X" tasks where the 20-node cap above would silently drop
# some of the true set.
_EXHAUSTIVE_FILE_CAP = 300
# Raw ripgrep fetch before path_glob filtering — must be well above
# _EXHAUSTIVE_FILE_CAP so a filter that excludes most hits (e.g. `!tests/*`
# on a repo with a huge test suite) doesn't get starved by an early cap.
_EXHAUSTIVE_RAW_CAP = 3000
# info(mode="source") caps, mirroring the host Read tool's own 2000-line cap.
_MAX_SOURCE_LINES = 2000
_MAX_DEPENDENTS_SHOWN = 5

# Test files auto-excluded from `search` when the agent hasn't scoped the
# query itself (no path_glob, query doesn't mention "test"). Traced runs
# repeatedly showed agents grinding through "list every file that calls X"
# tasks because a capped result filled up with test call-sites before the
# real (non-test) answer, without the agent ever thinking to ask for
# `path_glob='!tests/*'` itself — codegraph's `explore` hard-drops the same
# class of file by default for exactly this reason. Opt back in with an
# explicit path_glob, or a query that says "test".
_LOW_VALUE_PATH_MARKERS = ("/test/", "/tests/", "/__tests__/", "/spec/")
_LOW_VALUE_NAME_PATTERNS = (
    "test_*.py",
    "*_test.py",
    "conftest.py",
    "*_test.go",
    "*.test.ts",
    "*.test.tsx",
    "*.test.js",
    "*.test.jsx",
    "*.spec.ts",
    "*.spec.tsx",
    "*.spec.js",
    "*.spec.jsx",
    "*_test.rs",
)


def _is_low_value_file(file_path: str | None) -> bool:
    """Report whether *file_path* looks like a test/spec file."""
    if not file_path:
        return False
    posix = file_path.replace("\\", "/").lower()
    if any(marker in posix for marker in _LOW_VALUE_PATH_MARKERS):
        return True
    name = posix.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(name, pat) for pat in _LOW_VALUE_NAME_PATTERNS)


def _query_wants_tests(query: str) -> bool:
    """Report whether the query itself already mentions tests."""
    return "test" in query.lower()


# Never return these as search hits: they are never the answer and only add
# noise (an FTS name match on a parameter/local/import misleads the agent). A
# minified/bundled line is also capped (_MAX_TEXT_LEN) so one 100KB line can't
# blow the stdio message limit.
_NOISE_KINDS = frozenset({"parameter", "variable", "import"})
_MIN_RERANK = 2  # skip the rerank embed when there's nothing to reorder


def _glob_match(file_path: str | None, path_glob: str | None) -> bool:
    """
    Report whether *file_path* satisfies *path_glob*, with ``!`` negation.

    Traced runs showed agents inventing exactly this — ``file:X``,
    ``!tests/*`` — as query syntax `search` didn't parse, and also passing a
    plain filename (``httpx/_client.py``, no ``*``) expecting an exact match —
    a bare no-wildcard string is only expanded to a recursive dir scope
    (``tests`` -> ``tests/**``) when its last segment has no extension, so a
    literal file path still matches instead of silently becoming a directory
    pattern with nothing under it. Matched against the file's absolute path,
    that path's tail (so a shorter repo-relative guess still hits), and its
    bare filename.
    """
    if not path_glob:
        return True
    if not file_path:
        return False
    negate = path_glob.startswith("!")
    pattern = path_glob[1:] if negate else path_glob
    last = pattern.rsplit("/", 1)[-1]
    if not any(c in pattern for c in "*?[") and "." not in last:
        pattern = pattern.rstrip("/") + "/**"
    rel = file_path.replace("\\", "/")
    matched = (
        fnmatch.fnmatch(rel, pattern)
        or fnmatch.fnmatch(rel, "*/" + pattern)
        or fnmatch.fnmatch(rel.rsplit("/", 1)[-1], pattern)
    )
    return not matched if negate else matched


def _read_files(paths: set[str]) -> dict[str, list[str] | None]:
    """Read each file once into its lines (sync; keeps I/O off the await)."""
    out: dict[str, list[str] | None] = {}
    for fp in paths:
        try:
            with Path(fp).open(encoding="utf-8", errors="replace") as f:
                out[fp] = f.readlines()
        except OSError:
            out[fp] = None
    return out


def _decl_line(
    text_lines: list[str] | None, span_json: str | None
) -> str | None:
    """Return the symbol's declaration line — its de-facto signature."""
    if not text_lines or not span_json:
        return None
    try:
        start = int(json.loads(span_json)[0])
    except (ValueError, TypeError, IndexError):
        return None
    if 1 <= start <= len(text_lines):
        return text_lines[start - 1].strip()[:200] or None
    return None


async def _sig_nodes(
    store: SqliteStore, rows: list[dict], limit: int
) -> tuple[list[SigNode], bool]:
    """
    Build SigNodes (ref + declaration line) for *rows*, capped at *limit*.

    Attaches each symbol's declaration line (``def f(...) -> R``) so the agent
    reads its shape inline and skips a follow-up info() call. One batch span
    lookup + one read per distinct file keeps it cheap versus many info()s.
    """
    capped = rows[:limit]
    spans = await store.get_node_spans(
        [r["id"] for r in capped if r.get("id")]
    )
    file_lines = _read_files(
        {r["file_path"] for r in capped if r.get("file_path")}
    )
    out: list[SigNode] = []
    for r in capped:
        sig = _decl_line(
            file_lines.get(r.get("file_path", "")),
            spans.get(r.get("id", "")),
        )
        out.append(SigNode.model_validate({**r, "signature": sig}))
    return out, len(rows) > limit


async def _content_matches(
    store: SqliteStore,
    workspace: Workspace,
    query: str,
    path_glob: str | None,
) -> tuple[dict[str, dict], list[TextMatch]]:
    """
    Grep *query* over file content, folding each hit into its enclosing symbol.

    Returns ({node_id: node_row}, [TextMatch for hits with no symbol]).
    """
    try:
        matches, _ = await _ripgrep(
            workspace.project_root,
            query,
            path_glob=path_glob,  # ripgrep parses `!`-negation natively
            ignore_case=False,
            cap=80,
        )
    except (FileNotFoundError, _GrepError):
        return {}, []

    by_file: dict[str, list[tuple[int, str]]] = {}
    for m in matches:
        by_file.setdefault(m.file_path, []).append((m.line, m.text))

    nodes: dict[str, dict] = {}
    text_only: list[TextMatch] = []
    for fp, hits in by_file.items():
        rows = await store.get_nodes_in_file(fp)
        spans: list[tuple[int, int, dict]] = []
        for r in rows:
            sj = r.get("span_json")
            if not sj:
                continue
            try:
                s = json.loads(sj)
                spans.append((int(s[0]), int(s[2]), r))
            except (ValueError, TypeError, IndexError):
                continue
        for line, text in hits:
            best: dict | None = None
            best_size: int | None = None
            for a, b, r in spans:
                if a <= line <= b and (
                    best_size is None or (b - a) < best_size
                ):
                    best, best_size = r, b - a
            if best is not None:
                row = dict(best)
                row["file_path"] = fp
                nodes.setdefault(row["id"], row)
            elif len(text_only) < _TEXT_MATCH_CAP:
                text_only.append(
                    TextMatch(
                        file_path=fp,
                        line=line,
                        text=text.strip()[:_MAX_TEXT_LEN],
                    )
                )
    return nodes, text_only


async def _rerank(
    store: SqliteStore,
    workspace: Workspace,
    query: str,
    ordered: list[dict],
    via: list[str],
) -> tuple[list[dict], list[str]]:
    """
    Order candidates by semantic relevance to *query* (selective, not blunt).

    model2vec — the CPU model we already use — scores each candidate's stored
    vector against the query, so when the response is clamped we keep the most
    relevant symbols, not the FTS tail. Exact-name matches stay on top; missing
    vectors / no semantic layer fall back to the incoming FTS order.
    """
    if len(ordered) < _MIN_RERANK or workspace.semantic is None:
        return ordered, via
    try:
        scores = await workspace.semantic.rank(
            store, query, [r["id"] for r in ordered]
        )
    except Exception:
        scores = {}
    if not scores:
        return ordered, via
    qlow = query.strip().lower()

    def _key(pair: tuple[dict, str]) -> tuple[int, float]:
        row, _how = pair
        # Exact-name match is the only hard override; otherwise order by
        # semantic relevance (so "solve dependencies" ranks solve_dependencies
        # above a partial FTS hit like solve_generator).
        exact = 0 if str(row.get("name", "")).lower() == qlow else 1
        return (exact, -scores.get(row["id"], -1.0))

    reranked = sorted(zip(ordered, via, strict=True), key=_key)
    return [p[0] for p in reranked], [p[1] for p in reranked]


def _est_chars(nodes: list[SigNode], text_matches: list[TextMatch]) -> int:
    """Rough serialized size of a search response (for the char ceiling)."""
    n = sum(
        len(x.signature or "") + len(x.qualified_name) + len(x.name) + 60
        for x in nodes
    )
    return n + sum(len(t.text) + 40 for t in text_matches)


def _repeat_hint(repeats: int) -> str | None:
    """Steer-back hint: a repeated call cannot get a different result."""
    if repeats < 1:
        return None
    return (
        "This exact call just repeated — the result is deterministic and "
        "won't change. If the answer isn't here, narrow with path_glob or "
        "different terms, try relations/info instead of searching again, "
        "or commit to what you already have."
    )


# A gentle hint alone didn't stop weak models from re-issuing the identical
# call a dozen+ times in traced runs — they simply didn't act on it. Once a
# call has repeated this many times (3rd+ identical occurrence), withhold
# the (already-seen, unchanging) data entirely and force a decision instead:
# per published agent-loop-breaking practice, an actual block — not just a
# stronger warning — is what gets a stuck agent to change strategy.
_HARD_BLOCK_REPEATS = 2


def _blocked_message(repeats: int) -> str:
    """Forced-choice message for a call blocked after too many repeats."""
    return (
        f"BLOCKED: this exact call has now repeated {repeats + 1} times "
        "with the same unchanging result — repeating it again will keep "
        "being blocked, so no data is returned this time. Either answer "
        "now with your best current evidence, or make a genuinely "
        "different call: a different query/symbol, added/changed "
        "path_glob, or a different tool (relations/info)."
    )


def _looks_literal(query: str) -> bool:
    """Report whether *query* reads like a source snippet, not a bare name."""
    return any(c in query for c in " (){}.,;:")


async def _exhaustive_search(
    workspace: Workspace,
    query: str,
    path_glob: str | None,
) -> SearchResult:
    """
    List EVERY distinct file matching *query* — no 20-node cap.

    For "list every file that calls/imports X" tasks: the ranked node cap in
    `tool_search` is a hard ceiling that silently drops files once the true
    match count exceeds it (a real fan-out — e.g. 270 real callers behind a
    non-test filter — not a bug in the cap itself). This bypasses ranking
    entirely and returns bare file paths from `rg --files-with-matches`,
    which is naturally bounded by distinct-file count, not raw hit count.
    """
    args_key = json.dumps(
        {"query": query, "path_glob": path_glob, "exhaustive": True},
        sort_keys=True,
    )
    repeats = workspace.note_call("search", args_key)
    if repeats >= _HARD_BLOCK_REPEATS:
        return SearchResult(
            error=_blocked_message(repeats), indexing=workspace.is_indexing
        )
    try:
        raw_paths, raw_truncated = await _ripgrep_files_only(
            workspace.project_root,
            query,
            ignore_case=False,
            cap=_EXHAUSTIVE_RAW_CAP,
        )
    except (FileNotFoundError, _GrepError) as exc:
        return SearchResult(
            error=str(exc),
            indexing=workspace.is_indexing,
            repeat_hint=_repeat_hint(repeats),
        )
    # Filter AFTER fetching (not via rg's own --glob): the raw cap must not
    # be spent before a filter like `!tests/*` narrows the set, or a
    # test-heavy repo could get capped down to nothing but test files.
    # Auto-exclude tests too when the agent hasn't scoped this itself — the
    # exact failure this mode exists for ("list every FILE that calls X")
    # is dominated by test call-sites unless something drops them.
    auto_exclude = path_glob is None and not _query_wants_tests(query)
    filtered = [
        p
        for p in raw_paths
        if _glob_match(p, path_glob)
        and not (auto_exclude and _is_low_value_file(p))
    ]
    truncated = raw_truncated or len(filtered) > _EXHAUSTIVE_FILE_CAP
    return SearchResult(
        files=filtered[:_EXHAUSTIVE_FILE_CAP],
        count=min(len(filtered), _EXHAUSTIVE_FILE_CAP),
        truncated=truncated,
        indexing=workspace.is_indexing,
        repeat_hint=_repeat_hint(repeats),
    )


async def tool_search(
    store: SqliteStore,
    workspace: Workspace,
    query: str,
    limit: int = _SEARCH_CAP,
    path_glob: str | None = None,
    exhaustive: bool = False,
) -> SearchResult:
    """
    Find graph nodes by NAME, CONTENT, or MEANING — the one way in.

    Returns navigable symbol nodes (pass any to relations/info), ranked
    name > content > meaning. Content hits outside a symbol come back as
    ``text_matches``, matched LITERALLY (not regex — no escaping needed for
    ``Request(`` or similar). This is grep, symbol search and semantic
    search folded into a single call whose results are always graph
    handles. Scope with ``path_glob`` (e.g. ``"tests/*"``, ``"*.ts"``,
    ``"!tests/*"`` to exclude a subtree) instead of repeating the same
    query — there is no other query syntax (no ``file:``/``content:``
    prefixes). Test files are left out by default unless you set
    ``path_glob`` yourself or the query mentions "test" — pass
    ``path_glob="tests/*"`` to search them specifically. Set
    ``exhaustive=True`` for "list every file that calls/imports X" —
    returns every matching file path (no signatures), uncapped by the
    normal top-N node limit. If ``note`` is set, your literal text matched
    nothing and every node below is a name/meaning guess — don't repeat the
    same text, simplify it instead.
    """
    if exhaustive:
        return await _exhaustive_search(workspace, query, path_glob)
    limit = min(limit, _SEARCH_CAP)  # clamp: keep the response small
    args_key = json.dumps(
        {"query": query, "limit": limit, "path_glob": path_glob},
        sort_keys=True,
    )
    repeats = workspace.note_call("search", args_key)
    if repeats >= _HARD_BLOCK_REPEATS:
        return SearchResult(
            error=_blocked_message(repeats), indexing=workspace.is_indexing
        )
    return await _search_nodes(
        store, workspace, query, limit, path_glob, repeats
    )


async def _search_nodes(
    store: SqliteStore,
    workspace: Workspace,
    query: str,
    limit: int,
    path_glob: str | None,
    repeats: int,
) -> SearchResult:
    """Do the actual name/content/meaning search — see `tool_search`."""
    # Auto-exclude test files when the agent hasn't scoped the query itself —
    # see _LOW_VALUE_PATH_MARKERS. Skipped when path_glob is set (the agent
    # already scoped it, respect that) or the query itself says "test".
    exclude_tests = path_glob is None and not _query_wants_tests(query)
    # A path_glob (or the built-in test-exclusion) filters after the fact, so
    # pull a wider pool first or a tight filter (e.g. excluding a huge tests/
    # tree) could starve `limit`.
    fetch_limit = (
        min(limit * 5, 200) if (path_glob or exclude_tests) else limit
    )
    ordered: list[dict] = []
    via: list[str] = []
    seen: set[str] = set()

    def add(row: dict, how: str) -> None:
        nid = row.get("id")
        if not nid or nid in seen or row.get("kind") in _NOISE_KINDS:
            return
        if not _glob_match(row.get("file_path"), path_glob):
            return
        if exclude_tests and _is_low_value_file(row.get("file_path")):
            return
        seen.add(nid)
        ordered.append(row)
        via.append(how)

    # 1) name (FTS) — most precise
    for r in await store.search_symbols(query, limit=fetch_limit):
        add(r, "name")
    # 2) content (grep -> enclosing symbol)
    content_nodes, text_only = await _content_matches(
        store, workspace, query, path_glob
    )
    for r in content_nodes.values():
        add(r, "content")
    # 3) meaning (embeddings) — only to fill in when name/content are thin
    if len(ordered) < limit and workspace.semantic is not None:
        try:
            resp = await workspace.semantic.search(
                store, query, top_k=fetch_limit
            )
            if resp.available:
                for hit in resp.hits:
                    node = await store.get_node(hit.node_id)
                    if node:
                        add(node, "meaning")
        except Exception:  # noqa: S110 — semantic is best-effort  # nosec: B110
            pass

    ordered, via = await _rerank(store, workspace, query, ordered, via)
    capped = ordered[:limit]
    nodes, truncated = await _sig_nodes(store, capped, limit)
    # Belt-and-suspenders: drop trailing nodes/text if the response is still
    # too big (long signatures / many hits), so it never re-sends a huge blob.
    over = False
    while nodes and _est_chars(nodes, text_only) > _MAX_RESPONSE_CHARS:
        if text_only:
            text_only.pop()
        else:
            nodes.pop()
        over = True
    status = await _aggregate_status(store, "ok", capped)
    via_shown = via[: len(nodes)]
    # Two independent signals that the query never matched real code, either
    # of which alone should warn: the query *looks* like a literal snippet
    # (the old check — misses bare words/identifiers with no punctuation,
    # e.g. "message=" or "foo_bar"), or — regardless of what the query looks
    # like — every node actually shown is a semantic guess (no name/content
    # hit at all backs it). Checking only the former let a 100%-guessed
    # result through silently whenever the query didn't happen to contain
    # one of a fixed set of punctuation characters.
    all_guessed = bool(via_shown) and all(h == "meaning" for h in via_shown)
    no_confirmed_hit = not content_nodes and not text_only
    note = None
    if no_confirmed_hit and (_looks_literal(query) or all_guessed):
        note = (
            f"No literal match for {query!r} anywhere in the code — every "
            "node below came from name/meaning matching, not a confirmed "
            "occurrence. If none look right, simplify the query (e.g. just "
            "the method or type name) instead of repeating this one."
        )
    return SearchResult(
        nodes=nodes,
        via=via_shown,
        text_matches=text_only,
        count=len(nodes),
        truncated=truncated or over or len(ordered) > limit,
        resolver_status=status,
        indexing=workspace.is_indexing,
        repeat_hint=_repeat_hint(repeats),
        note=note,
    )


def _trim_relation_lists(*lists: list[SigNode]) -> bool:
    """Belt-and-suspenders: drop from the biggest list until under the cap."""
    trimmed = False

    def _size(lst: list[SigNode]) -> int:
        return _est_chars(lst, []) if lst else -1

    while sum(_est_chars(lst, []) for lst in lists) > _MAX_RESPONSE_CHARS:
        biggest = max(lists, key=_size)
        if not biggest:
            break
        biggest.pop()
        trimmed = True
    return trimmed


async def tool_relations(
    store: SqliteStore,
    workspace: Workspace,
    symbol: str,
    depth: int = 2,
    limit: int = _REL_CAP,
    file: str | None = None,
) -> RelationsResult:
    """
    Who calls a symbol, what it calls, and what implements it — one call.

    Accepts a node id OR a name. Returns the connected nodes (callers,
    callees, implementors, references); read any of them with ``info``. Each
    list is capped; the matching ``*_total`` field is the true count, so a
    hidden tail shows up as a number. A bigger ``limit`` cannot raise this —
    it's the graph's real size — so for an exhaustive enumeration beyond the
    cap, use ``search`` with distinguishing terms instead of re-calling this.
    Test-file callers/callees are left out by default (unless ``symbol``
    itself mentions "test") so the cap fills with production code, not test
    call-sites. Pass ``file`` (a path or suffix) to pin the right definition
    when several same-named symbols exist (e.g. one ``UserService`` per
    service in a monorepo). An empty ``callers`` does NOT by itself mean
    unused — a symbol invoked only through a JSX tag, a route decorator, or a
    DI container (e.g. ``Depends(...)``) has no direct-call edge at all and
    shows up in ``references`` instead; when that's the case ``note`` is set
    so you don't have to remember to check.
    """
    args_key = json.dumps(
        {"symbol": symbol, "depth": depth, "limit": limit, "file": file},
        sort_keys=True,
    )
    repeats = workspace.note_call("relations", args_key)
    if repeats >= _HARD_BLOCK_REPEATS:
        return RelationsResult(
            error=_blocked_message(repeats), indexing=workspace.is_indexing
        )
    hint = _repeat_hint(repeats)

    node_id = await _resolve_node_id(store, symbol, file)
    if node_id is None:
        return RelationsResult(
            error=(
                f"No symbol matches {symbol!r}. Find one with search first."
            ),
            indexing=workspace.is_indexing,
            repeat_hint=hint,
        )
    node = await store.get_node(node_id)
    if node is None:
        return RelationsResult(
            error=f"No symbol matches {symbol!r}.",
            indexing=workspace.is_indexing,
            repeat_hint=hint,
        )
    limit = min(limit, _REL_CAP)  # clamp per-list size
    d = min(depth, 5)
    exclude_tests = not _query_wants_tests(symbol)

    def _drop_tests(rows: list[dict]) -> list[dict]:
        if not exclude_tests:
            return rows
        return [r for r in rows if not _is_low_value_file(r.get("file_path"))]

    callers = _drop_tests(await store.get_callers(node_id, max_depth=d))
    callees = _drop_tests(await store.get_callees(node_id, max_depth=d))
    implementors = _drop_tests(
        await store.get_implementors(node_id, max_depth=d)
    )
    references = _drop_tests(await store.find_references(node_id))
    caller_refs, t1 = await _sig_nodes(store, callers, limit)
    callee_refs, t2 = await _sig_nodes(store, callees, limit)
    impl_refs, t3 = await _sig_nodes(store, implementors, limit)
    ref_refs, t4 = await _sig_nodes(store, references, limit)
    over = _trim_relation_lists(caller_refs, callee_refs, impl_refs, ref_refs)
    base = await _fresh_status(workspace, node)
    status = await _aggregate_status(
        store, base, callers + callees + implementors + references
    )
    note = None
    if not callers and references:
        note = (
            "callers is empty, but references is not — this symbol has no "
            "direct-call site, yet something in the code still refers to "
            "it (a JSX tag, a route decorator, a DI container like "
            "Depends(...), a passed-as-value callback, …). Check references "
            "before concluding this is unused."
        )
    return RelationsResult(
        node=NodeRef.from_row(node),
        callers=caller_refs,
        callees=callee_refs,
        implementors=impl_refs,
        references=ref_refs,
        callers_total=len(callers),
        callees_total=len(callees),
        implementors_total=len(implementors),
        references_total=len(references),
        resolver_status=status,
        truncated=t1 or t2 or t3 or t4 or over,
        indexing=workspace.is_indexing,
        repeat_hint=hint,
        note=note,
    )


async def _resolve_file_path(
    store: SqliteStore, workspace: Workspace, target: str
) -> Path | None:
    """
    Resolve *target* to an indexed file, tolerating a repo-name prefix.

    A raw join against the project root misses when the repo and its
    top-level package share a name (e.g. httpx/httpx) — the index root is
    already the package dir, but the model naturally writes the repo-relative
    path. Falls back to a suffix match among indexed files; ambiguous
    matches are left unresolved rather than guessed.
    """
    direct = _resolve_in_project(workspace, target)
    if direct.is_file():
        return direct
    suffix = target.strip("/").replace("\\", "/")
    if not suffix:
        return None
    candidates = await store.find_files_by_suffix(suffix)
    if len(candidates) == 1:
        return Path(candidates[0])
    return None


async def _file_source_info(
    store: SqliteStore,
    workspace: Workspace,
    abs_path: str,
    status: str,
    offset: int,
    limit: int,
) -> InfoResult:
    r"""
    Read *abs_path* as line-numbered source, Read-compatible.

    Same ``<n>\\t<line>`` shape Read gives you, safe to Edit from — plus
    which files import it (a one-line blast radius), so reading a file
    through ``info`` tells you what depends on it without a separate
    ``relations`` call.
    """
    try:
        with Path(abs_path).open(  # noqa: ASYNC230 — one cheap bounded read
            encoding="utf-8", errors="replace"
        ) as f:
            lines = f.readlines()
    except OSError as exc:
        return InfoResult(
            error=f"Could not read {abs_path}: {exc}",
            indexing=workspace.is_indexing,
        )
    total = len(lines)
    start = max(offset, 1)
    cap = min(limit, _MAX_SOURCE_LINES) if limit > 0 else _MAX_SOURCE_LINES
    end = min(start + cap - 1, total)
    body = "\n".join(
        f"{i}\t{lines[i - 1].rstrip(chr(10))}" for i in range(start, end + 1)
    )
    if end < total:
        body += (
            f"\n… (+{total - end} more lines — pass offset={end + 1} "
            "to continue)"
        )
    dependents = sorted(await store.get_importer_files(abs_path))
    return InfoResult(
        file_path=abs_path,
        source=body,
        dependents=dependents[:_MAX_DEPENDENTS_SHOWN],
        dependents_total=len(dependents),
        resolver_status=status,
        truncated=end < total,
        indexing=workspace.is_indexing,
    )


async def tool_info(
    store: SqliteStore,
    workspace: Workspace,
    target: str,
    limit: int = 200,
    file: str | None = None,
    mode: str = "outline",
    offset: int = 1,
) -> InfoResult:
    r"""
    Read a specific target: a SYMBOL's source/signature, or a FILE.

    Accepts a node id, a symbol name, or a file path (repo-relative or
    suffix, e.g. ``pkg/mod.py`` or ``mod.py``). Symbol -> source + signature
    + location. File -> its symbol outline by default (cheap structural
    overview); set ``mode="source"`` to instead read the file's current
    on-disk content with line numbers — the same ``<n>\\t<line>`` shape Read
    gives you, safe to Edit from, windowable with ``offset``/``limit`` just
    like Read — plus which files import it. Use ``mode="source"`` instead of
    a separate file read whenever you need the actual body, not just its
    symbol list. If a symbol name resolves to several definitions, pass
    ``file`` (a path or suffix) to pin the one you mean (e.g. one
    ``UserService`` per service in a monorepo).
    """
    args_key = json.dumps(
        {
            "target": target,
            "limit": limit,
            "file": file,
            "mode": mode,
            "offset": offset,
        },
        sort_keys=True,
    )
    repeats = workspace.note_call("info", args_key)
    if repeats >= _HARD_BLOCK_REPEATS:
        return InfoResult(
            error=_blocked_message(repeats), indexing=workspace.is_indexing
        )
    hint = _repeat_hint(repeats)
    # File mode first: a real file path wants the outline, even if a same-named
    # file-node would also resolve as a symbol.
    resolved = await _resolve_file_path(store, workspace, target)
    if resolved is not None:
        abs_path = str(resolved)
        status = await workspace.ensure_fresh(resolved)
        if mode == "source":
            result = await _file_source_info(
                store, workspace, abs_path, status, offset, limit
            )
            result.repeat_hint = hint
            return result
        rows = await store.get_nodes_in_file(abs_path)
        nodes, truncated = to_file_nodes(rows, min(limit, _FILE_OUTLINE_CAP))
        return InfoResult(
            file_path=abs_path,
            file_nodes=nodes,
            resolver_status=status,
            truncated=truncated,
            indexing=workspace.is_indexing,
            repeat_hint=hint,
        )

    node_id = await _resolve_node_id(store, target, file)
    if node_id is not None:
        node = await store.get_node(node_id)
        if node is not None:
            status = await _fresh_status(workspace, node)
            node = await store.get_node(node_id) or node
            return InfoResult(
                node=NodeRef.from_row(node),
                source=_read_span(
                    node.get("file_path"), node.get("span_json")
                ),
                repeat_hint=hint,
                signature=_first_meta(
                    node.get("metadata_json"), _SIGNATURE_KEYS
                ),
                docstring=_first_meta(
                    node.get("metadata_json"), _DOCSTRING_KEYS
                ),
                resolver_status=status,
                indexing=workspace.is_indexing,
            )

    return InfoResult(
        error=(
            f"No symbol or file matches {target!r}. Use search to find one."
        ),
        indexing=workspace.is_indexing,
        repeat_hint=hint,
    )
