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
    resolver_status: dict[str, str]
