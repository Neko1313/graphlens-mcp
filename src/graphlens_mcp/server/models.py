"""Pydantic models for the MCP tool boundary.

Typed request/response models give the agent a stable, self-describing contract
(FastMCP derives the tool output schema from these) and a single response envelope
so every tool reports graph quality (``resolver_status``) and truncation the same way.
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


class GraphResult(BaseModel):
    """Standard envelope for tools that return a list of nodes."""

    nodes: list[NodeRef] = Field(default_factory=list)
    count: int = 0
    resolver_status: str = "ok"  # ok | degraded | skeleton
    truncated: bool = False
    error: str | None = None


class NodeInfoResult(BaseModel):
    """Full info for a single node, including its source snippet."""

    node: NodeRef | None = None
    source: str | None = None
    resolver_status: str = "ok"
    error: str | None = None


class FileStructureResult(BaseModel):
    """Symbol outline of a file."""

    path: str
    nodes: list[NodeRef] = Field(default_factory=list)
    resolver_status: str = "ok"
    truncated: bool = False
    error: str | None = None


def to_refs(rows: list[dict[str, Any]], limit: int) -> tuple[list[NodeRef], bool]:
    """Convert store rows to NodeRefs, capped at *limit*. Returns (refs, truncated)."""
    capped = min(limit, MAX_RESULTS)
    truncated = len(rows) > capped
    return [NodeRef.from_row(r) for r in rows[:capped]], truncated
