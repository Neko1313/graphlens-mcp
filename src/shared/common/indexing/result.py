from collections.abc import Awaitable, Callable
from dataclasses import dataclass

__all__ = ["IndexResult", "ProgressCallback"]

ProgressCallback = Callable[[float, float, str], Awaitable[None]]
"""(done, total, message) — drives MCP ``ctx.report_progress``."""


@dataclass(slots=True)
class IndexResult:
    """What one ``index_project_graph`` run produced."""

    languages: list[str]
    files: int
    nodes: int
    relations: int
    embedded: int
    """Embeddable nodes (re)embedded this run — only the added/changed ones."""
    resolver_status: dict[str, str]
    reused: int = 0
    """Embeddable nodes unchanged since last index (re-embedding skipped)."""
    deleted: int = 0
    """Nodes that vanished since last index (removed from graph + vectors)."""
    unresolved: int = 0
    """References the resolver couldn't bind to any definition (diagnostics).

    graphlens's authoritative count (a query whose ref is None) — not the
    stdlib/third-party symbols it resolves to an external node.
    """
    ref: str | None = None
    """The git ref this run was recorded under, when the checkout is a repo."""
    changes: dict[str, int] | None = None
    """Temporal-log delta appended: created/updated/deleted/unchanged."""
