"""Tool arguments: the descriptions the model reads, and what we validate.

Every description lives here as a constant because it is stated twice — once on
the MCP tool's flat parameter (what the model actually sees in the schema) and
once on the params model the handler validates with. The tools take **flat**
parameters, not a single nested object: a `BaseModel` parameter lands in the
schema as a `$defs` reference, and models routinely answered it with flat
arguments anyway, burning a call and a retry on the rejection before getting
the shape right.
"""

from typing import Literal

from pydantic import BaseModel, Field, SecretStr, model_validator

__all__ = [
    "IndexParams",
    "InfoParams",
    "RelationsParams",
    "RemoveProjectParams",
    "SearchParams",
]

# The two time-travel knobs, worded once so both tools describe them alike.
HISTORY_REF = (
    "Read history on this branch/ref instead of the live graph. Omit for the "
    "current code. The project resource lists the refs indexed."
)
HISTORY_AT = (
    "Read the project as of this point: a commit sha (a unique prefix is "
    "enough) or a seq from the project's history. Omit for the ref's newest "
    "indexed commit."
)

PROJECT = (
    "Project id. Omit it — the server resolves the indexed project itself. "
    "Pass one only after a call has told you several are indexed; never call "
    "list_projects just to find it."
)
FILE_DISAMBIGUATOR = "Disambiguates an ambiguous symbol name by its file."

# --- info ---
INFO_TARGET = "A symbol (node id or name) or a file path to read."
INFO_MODE = (
    "For a file target: 'outline' (its symbols, the default) or 'source' "
    "(full text + importers). Ignored for a symbol target."
)
INFO_LIMIT = "With mode='source': max number of lines to return."
INFO_OFFSET = "With mode='source': first line to return (0-based)."
INFO_AT = (
    HISTORY_AT + " Source text is not stored per revision, so a past revision "
    "returns the symbol's recorded metadata without its body."
)

# --- relations ---
RELATIONS_SYMBOL = "A node id or a symbol name to find relations for."
RELATIONS_DEPTH = (
    "Call-graph hops for callers/callees (1–5). Default 1 — the DIRECT "
    "callers/callees, which is what 'who calls X' / 'which files call X' "
    "asks. Raise it only to trace transitively (X's callers' callers); a "
    "higher depth mixes indirect neighbours into the same list and is the "
    "wrong answer to a direct-callers question."
)
RELATIONS_LIMIT = "Max members per group; *_total is the true count."
RELATIONS_KINDS = (
    "Comma-separated groups to narrow to: calls, inherits_from, references. "
    "Empty means all four navigation groups."
)
RELATIONS_AT = (
    HISTORY_AT + " Neighbours are then the ones recorded at that point, not "
    "today's."
)

# --- search ---
SEARCH_QUERY = "What to find — by meaning, name, or literal content."
SEARCH_LIMIT = "Max number of hits to return."
SEARCH_PATH_GLOB = (
    "Scope to matching paths, e.g. 'src/**/*.py'. Literal, not regex; test "
    "files are excluded unless the glob includes them."
)
SEARCH_VERBOSITY = (
    "'concise' (the default) gives one line per hit — name, signature, "
    "path:line, and the id to pass to info/relations, which is usually "
    "already the answer; 'detailed' also inlines each hit's source."
)
SEARCH_EXHAUSTIVE = "List every in-scope file path instead of ranked hits."

# --- index / remove ---
INDEX_DIRECTORY = (
    "A local checkout to index. Must be a git repo with a remote (identity is "
    "hash(remote)); the whole repository is indexed. Mutually exclusive with "
    "repo_url."
)
INDEX_REPO_URL = (
    "A remote repository to clone and index (server/CI use). Mutually "
    "exclusive with directory."
)
INDEX_REF = (
    "Branch/ref to index. With repo_url: which branch to clone (defaults to "
    "the remote's HEAD). With a local directory: informational — the "
    "checkout's current ref is used."
)
INDEX_CREDENTIAL = (
    "Git token to clone a private repo_url. Sensitive — never logged. Ignored "
    "for a local directory."
)
INDEX_NAME = "Project name; defaults to the repository's name."
INDEX_DESCRIPTION = "Short description of the project (optional)."
REMOVE_PROJECT = "The id of the project to remove from the index."


class InfoParams(BaseModel):
    """Arguments for the ``info`` tool."""

    target: str = Field(description=INFO_TARGET)
    project: str | None = Field(default=None, description=PROJECT)
    mode: Literal["outline", "source"] = Field(
        default="outline",
        description=INFO_MODE,
    )
    limit: int | None = Field(default=None, description=INFO_LIMIT)
    offset: int = Field(default=0, description=INFO_OFFSET)
    file: str = Field(default="", description=FILE_DISAMBIGUATOR)
    ref: str = Field(default="", description=HISTORY_REF)
    at: str = Field(default="", description=INFO_AT)


class RelationsParams(BaseModel):
    """Arguments for the ``relations`` tool."""

    symbol: str = Field(description=RELATIONS_SYMBOL)
    project: str | None = Field(default=None, description=PROJECT)
    depth: int = Field(default=1, description=RELATIONS_DEPTH)
    limit: int = Field(default=25, description=RELATIONS_LIMIT)
    kinds: str = Field(default="", description=RELATIONS_KINDS)
    file: str = Field(default="", description=FILE_DISAMBIGUATOR)
    ref: str = Field(default="", description=HISTORY_REF)
    at: str = Field(default="", description=RELATIONS_AT)


class SearchParams(BaseModel):
    """Arguments for the ``search`` tool."""

    query: str = Field(description=SEARCH_QUERY)
    project: str | None = Field(default=None, description=PROJECT)
    limit: int = Field(default=25, description=SEARCH_LIMIT)
    path_glob: str | None = Field(default=None, description=SEARCH_PATH_GLOB)
    verbosity: Literal["concise", "detailed"] = Field(
        default="concise",
        description=SEARCH_VERBOSITY,
    )
    exhaustive: bool = Field(default=False, description=SEARCH_EXHAUSTIVE)


class IndexParams(BaseModel):
    """Arguments for the ``index`` tool — exactly one source.

    Local use passes ``directory`` (an on-disk checkout); server/CI use passes
    ``repo_url`` (cloned to a tmp dir). Re-running ``index`` on the same source
    refreshes that project in place — there is no separate refresh tool.
    """

    directory: str | None = Field(default=None, description=INDEX_DIRECTORY)
    repo_url: str | None = Field(default=None, description=INDEX_REPO_URL)
    ref: str | None = Field(default=None, description=INDEX_REF)
    ci_token: SecretStr | None = Field(
        default=None,
        description=INDEX_CREDENTIAL,
    )
    name: str | None = Field(default=None, description=INDEX_NAME)
    description: str | None = Field(
        default=None,
        description=INDEX_DESCRIPTION,
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

    project: str = Field(description=REMOVE_PROJECT)
