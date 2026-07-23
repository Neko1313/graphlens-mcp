from typing import Literal

from pydantic import BaseModel, Field, SecretStr, model_validator

__all__ = [
    "IndexParams",
    "InfoParams",
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


class IndexParams(BaseModel):
    """Arguments for the ``index`` tool — exactly one source.

    Local use passes ``directory`` (an on-disk checkout); server/CI use passes
    ``repo_url`` (cloned to a tmp dir). Re-running ``index`` on the same source
    refreshes that project in place — there is no separate refresh tool.
    """

    directory: str | None = Field(
        default=None,
        description="A local checkout to index. Must be a git repo with a "
        "remote (identity is hash(remote)); the whole repository is indexed. "
        "Mutually exclusive with repo_url.",
    )
    repo_url: str | None = Field(
        default=None,
        description="A remote repository to clone and index (server/CI use). "
        "Mutually exclusive with directory.",
    )
    ref: str | None = Field(
        default=None,
        description="Branch/ref to index. With repo_url: which branch to "
        "clone (defaults to the remote's HEAD). With a local directory: "
        "informational — the checkout's current ref is used.",
    )
    ci_token: SecretStr | None = Field(
        default=None,
        description="Git token to clone a private repo_url. Sensitive — never "
        "logged. Ignored for a local directory.",
    )
    name: str | None = Field(
        default=None,
        description="Project name; defaults to the repository's name.",
    )
    description: str | None = Field(
        default=None,
        description="Short description of the project (optional).",
    )

    @model_validator(mode="after")
    def _one_source(self) -> "IndexParams":
        # Normalize blank strings to None so a client sending "" for the
        # unused source can't diverge the exactly-one check from the
        # downstream `is not None` dispatch.
        self.directory = self.directory or None
        self.repo_url = self.repo_url or None
        if (self.directory is None) == (self.repo_url is None):
            msg = "provide exactly one of 'directory' or 'repo_url'"
            raise ValueError(msg)
        return self


class RemoveProjectParams(BaseModel):
    """Arguments for the ``remove_project`` tool."""

    project: str = Field(
        description="The id of the project to remove from the index.",
    )
