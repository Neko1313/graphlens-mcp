from typing import Any, Literal

from pydantic import BaseModel, Field

__all__ = [
    "Candidates",
    "FileOutline",
    "FileSource",
    "NodeInfo",
    "NodeRef",
    "OutlineEntry",
    "RelationsResult",
    "SearchHit",
    "SearchResult",
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


class RelationsResult(BaseModel):
    """A symbol's neighbours, split into the four navigation groups."""

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
