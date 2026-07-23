from pathlib import Path

from pydantic import BaseModel, ConfigDict

__all__ = ["Project"]


class Project(BaseModel):
    """A registered project: its identity plus the metadata resources expose.

    Storage-agnostic — persisted by value via ``model_dump(mode="json")`` and
    rehydrated with ``model_validate``, so any store (Kuzu node props, a KV
    blob, a JSON column) can hold it without an ORM binding.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    """Stable identity = ``hash(git remote + subpath)``.

    Doubles as the isolation key: the ``project_id`` scalar filtered on every
    graph and vector read/write. Not path-derived — two clones of the same
    repo+subpath share it.
    """
    name: str
    path: Path
    subpath: str = ""
    """The indexed subdirectory within the repo (``""`` = the whole repo).

    Part of the identity, and the scope re-index restricts to.
    """
    description: str | None = None
    git_url: str | None = None
