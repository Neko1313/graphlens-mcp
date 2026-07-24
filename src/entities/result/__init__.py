from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from entities.project import Project

__all__ = [
    "AlreadyCurrent",
    "Cancelled",
    "Candidates",
    "Declined",
    "FileOutline",
    "FileSource",
    "IndexProjectResult",
    "Indexed",
    "InfoResult",
    "NodeInfo",
    "NodeRef",
    "NotFound",
    "OutlineEntry",
    "ProjectNotFound",
    "RelationsLookup",
    "RelationsResult",
    "RemoveProjectResult",
    "Removed",
    "Revision",
    "SearchHit",
    "SearchResult",
    "Skipped",
]


class Revision(BaseModel):
    """Which point in history a result was answered from.

    Present only on a time-travel read (``ref``/``at``); its absence means the
    answer came from the live graph.
    """

    ref: str
    seq: int
    """The log's own index-order counter for the ref — not a git commit depth.

    Usable verbatim as a later ``at`` value.
    """
    sha: str | None = None
    time_update: int | None = None
    """Committer timestamp (epoch seconds) of the commit at this point."""
    is_head: bool = False
    """Whether this is the newest indexed commit on the ref."""
    note: str = ""
    """What this point cannot answer — e.g. that source text isn't stored."""


class NodeRef(BaseModel):
    """The identity of a graph node, without its body."""

    id: str
    name: str
    qualified_name: str
    kind: str
    file_path: str | None = None


class NodeInfo(NodeRef):
    """A node with its source, signature, and adapter metadata."""

    type: Literal["node"] = "node"
    signature: str = ""
    source: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    revision: Revision | None = None


class OutlineEntry(BaseModel):
    """One symbol in a file outline."""

    id: str
    name: str
    qualified_name: str
    kind: str
    line: int


class FileOutline(BaseModel):
    """A file's declared symbols and the files that import from it."""

    type: Literal["outline"] = "outline"
    file_path: str
    symbols: list[OutlineEntry]
    importers: list[str] = Field(default_factory=list)
    revision: Revision | None = None


class FileSource(BaseModel):
    """A file's source text (optionally a line window) and its importers."""

    type: Literal["file"] = "file"
    file_path: str
    source: str
    importers: list[str] = Field(default_factory=list)


class Candidates(BaseModel):
    """Returned when a name resolves to several symbols."""

    type: Literal["candidates"] = "candidates"
    candidates: list[NodeRef]


class NotFound(BaseModel):
    """The target didn't resolve to a node or a file."""

    type: Literal["not_found"] = "not_found"
    target: str


InfoResult = Annotated[
    NodeInfo | FileOutline | FileSource | Candidates | NotFound,
    Field(discriminator="type"),
]
"""The wire result of ``info`` — a discriminated union keyed on ``type``."""


class RelationsResult(BaseModel):
    """A symbol's neighbours, split into the four navigation groups."""

    type: Literal["relations"] = "relations"
    node: NodeRef
    depth: int
    kinds: list[str]
    callers: list[NodeRef] = Field(default_factory=list)
    callers_total: int = 0
    callees: list[NodeRef] = Field(default_factory=list)
    callees_total: int = 0
    callees_unresolved: int = 0
    implementors: list[NodeRef] = Field(default_factory=list)
    implementors_total: int = 0
    references: list[NodeRef] = Field(default_factory=list)
    references_total: int = 0
    not_indexed: list[str] = Field(
        default_factory=list,
        description=(
            "Groups this project has NO edges for at all, because its "
            "language analyzer does not produce them (Rust emits no "
            "inherits_from, Go no references). An empty group listed here "
            "means 'unknown', not 'none' — do not read it as an answer, and "
            "do not re-query hoping for a different one."
        ),
    )
    revision: Revision | None = None


RelationsLookup = Annotated[
    RelationsResult | Candidates | NotFound,
    Field(discriminator="type"),
]
"""The wire result of ``relations`` — discriminated union keyed on ``type``."""


class SearchHit(BaseModel):
    """One search result: how it was found, and where to read it."""

    id: str
    name: str
    kind: str
    file_path: str
    signature: str = ""
    score: float | None = None
    match: str = "semantic"
    """How it was found: semantic | name | content | file."""
    line: int | None = None
    uri: str = ""


class SearchResult(BaseModel):
    """A search response: the hits, plus the project they came from."""

    project: str
    query: str
    hits: list[SearchHit] = Field(default_factory=list)


# --- project management outcomes (discriminated on ``status``) ---


class Indexed(BaseModel):
    """A project was indexed (or re-indexed) in full."""

    status: Literal["indexed"] = "indexed"
    project: Project
    languages: list[str]
    files: int
    nodes: int
    relations: int
    embedded: int
    resolver_status: dict[str, str]
    reused: int = 0
    deleted: int = 0
    unresolved: int = 0
    ref: str | None = None
    changes: dict[str, int] | None = None


class AlreadyCurrent(BaseModel):
    """A server-mode index was skipped: the ref's HEAD is already indexed.

    Resolved from ``git ls-remote`` against the last-indexed sha, so no clone
    was performed.
    """

    status: Literal["already_current"] = "already_current"
    project: str
    ref: str
    sha: str


class Removed(BaseModel):
    """A project's graph, vectors, and registry entry were deleted."""

    status: Literal["removed"] = "removed"
    project: Project


class Skipped(BaseModel):
    """The user was asked to confirm and answered no."""

    status: Literal["skipped"] = "skipped"
    reason: str = ""


class Declined(BaseModel):
    """The client declined the elicitation."""

    status: Literal["declined"] = "declined"


class Cancelled(BaseModel):
    """The client cancelled the elicitation."""

    status: Literal["cancelled"] = "cancelled"


class ProjectNotFound(BaseModel):
    """The named project isn't registered."""

    status: Literal["not_found"] = "not_found"
    project: str


IndexProjectResult = Annotated[
    Indexed | AlreadyCurrent | Skipped | Declined | Cancelled,
    Field(discriminator="status"),
]
RemoveProjectResult = Annotated[
    Removed | ProjectNotFound | Skipped | Declined | Cancelled,
    Field(discriminator="status"),
]
