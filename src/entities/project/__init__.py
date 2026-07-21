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
    """Stable, path-derived identifier — also the vector collection name."""
    name: str
    path: Path
    description: str | None = None
    git_url: str | None = None
