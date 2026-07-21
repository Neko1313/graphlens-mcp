from typing import Literal

from pydantic import BaseModel, Field

__all__ = [
    "IndexProjectParams",
    "InfoParams",
    "RefreshProjectParams",
    "RelationsParams",
    "RemoveProjectParams",
    "SearchParams",
]


class InfoParams(BaseModel):
    """Arguments for the ``info`` tool."""

    target: str = Field(
        description="A symbol (node id or name) or a file path to read.",
    )
    project: str | None = Field(
        default=None,
        description="Project id — needed only when more than one is indexed.",
    )
    mode: Literal["outline", "source"] = Field(
        default="outline",
        description=(
            "For a file target: 'outline' (its symbols, the default) or "
            "'source' (full text + importers). Ignored for a symbol target."
        ),
    )
    limit: int | None = Field(
        default=None,
        description="With mode='source': max number of lines to return.",
    )
    offset: int = Field(
        default=0,
        description="With mode='source': first line to return (0-based).",
    )
    file: str = Field(
        default="",
        description="Disambiguates an ambiguous symbol name by its file.",
    )


class RelationsParams(BaseModel):
    """Arguments for the ``relations`` tool."""

    symbol: str = Field(
        description="A node id or a symbol name to find relations for.",
    )
    project: str | None = Field(
        default=None,
        description="Project id — needed only when more than one is indexed.",
    )
    depth: int = Field(
        default=2,
        description="How many hops to follow for callers/callees (1–5).",
    )
    limit: int = Field(
        default=25,
        description="Max members per group; *_total is the true count.",
    )
    kinds: str = Field(
        default="",
        description=(
            "Comma-separated groups to narrow to: calls, inherits_from, "
            "references. Empty means all four navigation groups."
        ),
    )
    file: str = Field(
        default="",
        description="Disambiguates an ambiguous symbol name by its file.",
    )


class SearchParams(BaseModel):
    """Arguments for the ``search`` tool."""

    query: str = Field(
        description="What to find — by meaning, name, or literal content.",
    )
    project: str | None = Field(
        default=None,
        description="Project id — needed only when more than one is indexed.",
    )
    limit: int = Field(
        default=25,
        description="Max number of hits to return.",
    )
    path_glob: str | None = Field(
        default=None,
        description="Scope to matching paths, e.g. 'src/**/*.py'. Literal, "
        "not regex; test files are excluded unless the glob includes them.",
    )
    verbosity: Literal["concise", "detailed"] = Field(
        default="concise",
        description=(
            "'concise' returns each hit's signature plus a resource link; "
            "'detailed' inlines the source."
        ),
    )
    exhaustive: bool = Field(
        default=False,
        description="List every in-scope file path instead of ranked hits.",
    )


class IndexProjectParams(BaseModel):
    """Arguments for the ``index_project`` tool."""

    path: str = Field(
        description="A directory to index into the code graph.",
    )
    name: str | None = Field(
        default=None,
        description="Project name; defaults to the directory name.",
    )
    description: str | None = Field(
        default=None,
        description="Short description of the project (optional).",
    )
    subpaths: list[str] | None = Field(
        default=None,
        description="Restrict indexing to these subdirectories (monorepo "
        "scope). Omit to index the whole directory.",
    )


class RefreshProjectParams(BaseModel):
    """Arguments for the ``refresh_project`` tool."""

    project: str = Field(
        description="The id of an already-registered project to re-index.",
    )


class RemoveProjectParams(BaseModel):
    """Arguments for the ``remove_project`` tool."""

    project: str = Field(
        description="The id of the project to remove from the index.",
    )
