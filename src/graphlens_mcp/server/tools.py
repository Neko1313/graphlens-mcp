"""
Shared resolution/read/grep helpers used by the lean MCP surface.

The 15-tool surface that used to live here was removed once the 3-tool
``search``/``relations``/``info`` prototype (:mod:`graphlens_mcp.server.lean`)
proved it could match its accuracy at a fraction of the tokens — see
``benchmarks/data`` and the project memory for the A/B numbers. What remains
is the name/path resolution, on-access freshness, and ripgrep plumbing that
:mod:`graphlens_mcp.server.lean` builds its three tools on top of.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from graphlens_mcp.server.models import CodeMatch
from graphlens_mcp.store.sqlite_store import SqliteStore, worst_status

if TYPE_CHECKING:
    from graphlens_mcp.indexer.workspace import Workspace

# ripgrep's exit code for a fatal error (e.g. an invalid regex); exit 1 means
# "no matches" which is not an error.
_RG_ERROR_EXIT = 2

# A single symbol's source is capped to this many lines before it is returned
# to the agent. A 600-line class body returned verbatim is mostly tokens the
# agent never needs (and can blow a tool result past the host's inline-result
# cap, forcing an externalize -> re-Read round-trip). The agent can always open
# the file for the full body; the cap keeps the common case lean.
MAX_SPAN_LINES = 200


def _read_span(
    path: str | None,
    span_json: str | None,
    max_lines: int = MAX_SPAN_LINES,
) -> str | None:
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
        if max_lines > 0 and len(snippet) > max_lines:
            dropped = len(snippet) - max_lines
            kept = "".join(snippet[:max_lines]).rstrip("\n")
            return (
                f"{kept}\n… (+{dropped} more lines truncated — "
                "open the file for the full body)"
            )
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


def _pick_node(
    rows: list[dict[str, Any]], file_hint: str | None = None
) -> dict[str, Any]:
    """
    Prefer a locally-defined node (file_path set) over an external stub.

    If *file_hint* (a path or suffix) is given and one candidate's file_path
    ends with it, that candidate wins outright — disambiguates a name that
    resolves to several definitions (e.g. one ``UserService`` per service in
    a monorepo) without the agent having to inspect every candidate first.
    """
    if file_hint:
        hint = file_hint.replace("\\", "/")
        for r in rows:
            fp = r.get("file_path")
            if fp and fp.replace("\\", "/").endswith(hint):
                return r
    return next((r for r in rows if r.get("file_path")), rows[0])


async def _resolve_node_id(
    store: SqliteStore, ref: str, file: str | None = None
) -> str | None:
    """
    Resolve *ref* — a node id OR a symbol name — to a node id.

    Tools accept either so the agent can call e.g.
    ``relations("create_order")`` directly without a separate search
    round-trip. Resolution order: exact node id, then an exact-case name
    match (so ``ResponseWriter`` picks the interface, not the FTS-adjacent
    ``responseWriter`` struct), then the best FTS match. *file* (a path or
    suffix) breaks a tie among same-named candidates in either of the last
    two steps. Returns None if nothing matches.
    """
    if not ref:
        return None
    if await store.get_node(ref) is not None:
        return ref
    # Widen the pool when disambiguating: a common name (e.g. "Dashboard")
    # can have far more than 20 exact-name matches, and the one the file
    # hint is looking for may sit past the default cutoff.
    exact_limit = 100 if file else 20
    exact = await store.find_nodes_by_exact_name(ref, limit=exact_limit)
    if exact:
        return _pick_node(exact, file)["id"]
    rows = await store.search_symbols(ref, limit=10 if file else 1)
    return _pick_node(rows, file)["id"] if rows else None


class _GrepError(Exception):
    """A search engine error (e.g. an invalid regular expression)."""


async def _ripgrep(
    root: Path,
    pattern: str,
    *,
    path_glob: str | None,
    ignore_case: bool,
    cap: int,
) -> tuple[list[CodeMatch], bool]:
    """
    Run ripgrep with JSON output, capped at *cap* matches.

    Fixed-strings (``-F``), not regex: `search`'s tool description never
    advertises regex, and agents overwhelmingly write literal snippets
    (``Request(``, ``getErrorMap(``) — as regex those are unbalanced groups
    that `rg` rejects outright. That failure used to be swallowed silently
    by the caller, so the agent saw empty content hits with no indication
    its own query was invalid syntax and kept grinding through rephrasings.
    """
    args = ["rg", "-F", "--json", "--no-messages"]
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


async def _ripgrep_files_only(
    root: Path,
    pattern: str,
    *,
    ignore_case: bool,
    cap: int,
) -> tuple[list[str], bool]:
    """
    List every distinct file containing a match — no per-line cap needed.

    Uses ``rg --files-with-matches``: one line per matching file regardless
    of how many times it matches, so a query with thousands of hits across a
    handful of files still returns cheaply and completely. This is what
    "list every file that calls/imports X" tasks need — `_ripgrep`'s
    per-line cap can miss whole files once a hot file exhausts it first.

    No ``path_glob`` here (unlike `_ripgrep`): ripgrep's own gitignore-style
    ``--glob`` anchors a slash-containing pattern to the search root and
    does NOT recurse into subdirectories the way a bare-dir shorthand like
    ``"tests"`` implies (``!tests/*`` misses ``tests/nested/foo.py``). The
    caller filters the returned paths with `_glob_match`'s bare-dir/negation
    handling instead, so exclusion is correct regardless of nesting depth.
    Fixed-strings (``-F``) for the same reason as `_ripgrep` — see there.
    """
    args = ["rg", "-F", "--files-with-matches", "--no-messages"]
    if ignore_case:
        args.append("-i")
    args += ["--glob", "!.graphlens", "--glob", "!.git"]
    args += ["-e", pattern, str(root)]

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    paths = sorted(
        p for p in stdout.decode("utf-8", "replace").splitlines() if p
    )
    truncated = len(paths) > cap
    if proc.returncode == _RG_ERROR_EXIT and not paths:
        msg = stderr.decode("utf-8", "replace").strip() or "search error"
        raise _GrepError(msg)
    return paths[:cap], truncated
