from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from entities.project import Project

__all__ = [
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
    "RefreshProjectResult",
    "Refreshed",
    "RelationsLookup",
    "RelationsResult",
    "RemoveProjectResult",
    "Removed",
    "SearchHit",
    "SearchResult",
    "Skipped",
]


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


class Refreshed(BaseModel):
    """An already-registered project was re-indexed from its stored path."""

    status: Literal["refreshed"] = "refreshed"
    project: str
    files: int
    nodes: int
    relations: int
    embedded: int
    resolver_status: dict[str, str]


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
    Indexed | Skipped | Declined | Cancelled,
    Field(discriminator="status"),
]
RefreshProjectResult = Annotated[
    Refreshed | ProjectNotFound,
    Field(discriminator="status"),
]
RemoveProjectResult = Annotated[
    Removed | ProjectNotFound | Skipped | Declined | Cancelled,
    Field(discriminator="status"),
]
