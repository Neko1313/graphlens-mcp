"""
Pydantic models for the MCP tool boundary.

Typed request/response models give the agent a stable, self-describing
contract (FastMCP derives the tool output schema from these) and a single
response envelope so every tool reports graph quality (``resolver_status``)
and truncation the same way.
"""

from __future__ import annotations

import json
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
    signature: str | None = None
    docstring: str | None = None
    resolver_status: str = "ok"
    error: str | None = None


class FileStructureResult(BaseModel):
    """Symbol outline of a file."""

    path: str
    nodes: list[NodeRef] = Field(default_factory=list)
    resolver_status: str = "ok"
    truncated: bool = False
    error: str | None = None


def to_refs(
    rows: list[dict[str, Any]], limit: int
) -> tuple[list[NodeRef], bool]:
    """Convert rows to NodeRefs, capped at *limit*. Returns (refs, trunc)."""
    capped = min(limit, MAX_RESULTS)
    truncated = len(rows) > capped
    return [NodeRef.from_row(r) for r in rows[:capped]], truncated


# ----------------------------------------------------------------------
# Semantic search / clusters (optional [semantic] extra)
# ----------------------------------------------------------------------


class CodeMatch(BaseModel):
    """A single line match from content (grep-style) search."""

    file_path: str
    line: int
    text: str


class CodeSearchResult(BaseModel):
    """Result of a content/regex search over the project's files."""

    matches: list[CodeMatch] = Field(default_factory=list)
    count: int = 0
    truncated: bool = False
    error: str | None = None


class SemanticHit(BaseModel):
    """
    A semantic search hit: a graph node matched by meaning.

    Each hit IS a graph node — pass ``node_id`` directly to
    get_callers / get_callees / get_node_info to pivot into the graph.
    """

    node_id: str
    kind: str
    name: str
    qualified_name: str
    file_path: str | None = None
    score: float


class SemanticResult(BaseModel):
    """
    Standard envelope for semantic search / find_related.

    ``available`` is False (with a ``reason``) when the optional extra is not
    installed or the embedding model cannot be fetched, so the agent can fall
    back to search_symbols / search_code instead of treating it as an error.
    """

    hits: list[SemanticHit] = Field(default_factory=list)
    count: int = 0
    available: bool = True
    truncated: bool = False
    reason: str | None = None
    error: str | None = None


class ClusterRef(BaseModel):
    """A semantic cluster: a labeled group of related symbols."""

    id: int
    label: str
    size: int
    terms: list[str] = Field(default_factory=list)


class ClusterList(BaseModel):
    """The list of semantic clusters across the codebase."""

    clusters: list[ClusterRef] = Field(default_factory=list)
    count: int = 0
    available: bool = True
    truncated: bool = False
    reason: str | None = None
    error: str | None = None


class ClusterInfo(BaseModel):
    """A single cluster with its member nodes."""

    cluster: ClusterRef | None = None
    members: list[NodeRef] = Field(default_factory=list)
    available: bool = True
    truncated: bool = False
    reason: str | None = None
    error: str | None = None


def cluster_ref_from_row(row: dict[str, Any]) -> ClusterRef:
    """Build a ClusterRef from a store row (terms stored as a JSON string)."""
    terms = row.get("terms")
    parsed: list[str] = []
    if isinstance(terms, str) and terms:
        try:
            loaded = json.loads(terms)
            if isinstance(loaded, list):
                parsed = [str(t) for t in loaded]
        except (ValueError, TypeError):
            parsed = []
    return ClusterRef(
        id=int(row["id"]),
        label=str(row["label"]),
        size=int(row["size"]),
        terms=parsed,
    )
