"""
Pydantic models for the MCP tool boundary.

Typed request/response models give the agent a stable, self-describing
contract (FastMCP derives the tool output schema from these) and a single
response envelope so every tool reports graph quality (``resolver_status``)
and truncation the same way.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# Max nodes any single tool will return, to protect the agent's context window.
MAX_RESULTS = 200


class NodeRef(BaseModel):
    """A reference to a graph node returned to the agent."""

    model_config = ConfigDict(extra="ignore")

    id: str
    kind: str
    qualified_name: str
    name: str
    file_path: str | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> NodeRef:
        """Build a NodeRef from a raw store row (extra columns ignored)."""
        return cls.model_validate(row)


class FileNode(BaseModel):
    """
    A symbol in a file outline — slightly leaner than NodeRef.

    A file outline can list ~1000 symbols, sent on every info(file) call
    (often several per task), so it drops NodeRef's ``file_path`` — always
    the queried file here, pure redundancy. ``qualified_name`` is KEPT: agents
    rely on it to disambiguate same-named symbols (dropping it measurably made
    HARD tasks loop more, not less).
    """

    model_config = ConfigDict(extra="ignore")

    id: str
    kind: str
    qualified_name: str
    name: str


class TextMatch(BaseModel):
    """A content hit outside any symbol (config, comment, non-code)."""

    file_path: str
    line: int
    text: str


class CodeMatch(BaseModel):
    """A single line match from content (grep-style) search."""

    file_path: str
    line: int
    text: str


class SigNode(NodeRef):
    """
    A node ref carrying its one-line signature.

    search/relations attach it so the agent can read a symbol's shape and
    often answer or disambiguate WITHOUT a follow-up info() call — the cheap
    inline context that stops search-returns-pointers turning into an
    info-grind on multi-symbol tasks.
    """

    signature: str | None = None


class SearchResult(BaseModel):
    """
    Unified search: graph NODES matched by name, content, or meaning.

    Unlike a grep that returns dead text lines, every content hit is folded
    into the symbol that encloses it, so each result is a navigable node the
    agent can pass straight to relations/info. ``via`` records how each node
    matched (name | content | meaning). ``text_matches`` holds hits with no
    enclosing symbol (config/comments) so raw-text lookups still work.
    """

    nodes: list[SigNode] = Field(default_factory=list)
    via: list[str] = Field(default_factory=list)
    text_matches: list[TextMatch] = Field(default_factory=list)
    count: int = 0
    truncated: bool = False
    resolver_status: str = "ok"
    indexing: bool = False
    error: str | None = None
    # Set only when this exact call (same query/limit/path_glob) was just
    # repeated verbatim — the result is deterministic, so repeating it again
    # cannot help; None the rest of the time (no cost on the common path).
    repeat_hint: str | None = None
    # Populated only when called with exhaustive=True: every distinct file
    # containing a match (path-only, no signatures) — for "list every file
    # that calls/imports X" tasks where `nodes`' cap would silently drop
    # some of the true set. Empty on the common (non-exhaustive) path.
    files: list[str] = Field(default_factory=list)
    # Set only when the query looked like literal source text (spaces,
    # punctuation) but matched NOTHING verbatim in the code — every node
    # below came from name/meaning matching instead, not a confirmed
    # occurrence. Traced runs showed agents re-issuing the same unmatched
    # literal text over and over, since a full page of plausible-looking
    # fallback results gives no sign the exact string was never found.
    note: str | None = None


class RelationsResult(BaseModel):
    """A symbol's neighbourhood: who calls it, what it calls, subtypes."""

    node: NodeRef | None = None
    callers: list[SigNode] = Field(default_factory=list)
    callees: list[SigNode] = Field(default_factory=list)
    implementors: list[SigNode] = Field(default_factory=list)
    references: list[SigNode] = Field(default_factory=list)
    # True totals before capping, so a hidden tail is visible as a number
    # ("15 shown of 22") instead of a bare truncated flag the agent has to
    # guess the size of and is tempted to re-request with a bigger `limit`
    # (which cannot reveal more — this is the size of the graph, not a page).
    callers_total: int = 0
    callees_total: int = 0
    implementors_total: int = 0
    references_total: int = 0
    resolver_status: str = "ok"
    truncated: bool = False
    indexing: bool = False
    error: str | None = None
    repeat_hint: str | None = None
    # Set only when callers is empty but references is not: a symbol invoked
    # through something other than a literal call (a JSX tag, a route
    # decorator, a DI container like FastAPI's Depends(...)) has no CALLS
    # edge to it at all — the graph correctly records the use as a
    # reference instead. callers==0 alone reads exactly like "unused", so
    # without this an agent doing impact/dead-code analysis on a
    # React/decorator-heavy codebase draws the wrong conclusion from a
    # technically-accurate empty list.
    note: str | None = None


class InfoResult(BaseModel):
    """
    Info about a specific target — a symbol OR a file.

    Symbol mode (target resolves to a node): source, signature, location.
    File mode (target is a file path): the file's symbol outline.
    """

    node: NodeRef | None = None
    source: str | None = None
    signature: str | None = None
    docstring: str | None = None
    file_path: str | None = None
    file_nodes: list[FileNode] = Field(default_factory=list)
    # File + mode="source" only: files that import this one (blast radius),
    # capped; dependents_total is the true count behind the cap.
    dependents: list[str] = Field(default_factory=list)
    dependents_total: int = 0
    resolver_status: str = "ok"
    truncated: bool = False
    indexing: bool = False
    error: str | None = None
    # Set only when this exact call (same target/limit/file/mode/offset) was
    # just repeated verbatim — the result is deterministic, so repeating it
    # again cannot help; None the rest of the time (no cost on the common
    # path).
    repeat_hint: str | None = None


def to_file_nodes(
    rows: list[dict[str, Any]], limit: int
) -> tuple[list[FileNode], bool]:
    """Convert rows to lean FileNodes, capped at *limit*; returns (nodes,t)."""
    capped = min(limit, MAX_RESULTS)
    truncated = len(rows) > capped
    return [FileNode.model_validate(r) for r in rows[:capped]], truncated
