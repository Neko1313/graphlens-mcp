from typing import Protocol

from entities.project import Project

__all__ = ["ProjectRegistry"]


class ProjectRegistry(Protocol):
    """The global project registry — the list every deployment shares.

    Deliberately separate from the per-project code graph so it survives a
    future move to per-project graph files. Kuzu-backed today; a Neo4j sibling
    would implement the same port.
    """

    async def ensure_schema(self) -> None:
        """Create the registry's node table if it isn't there yet."""
        ...

    async def add(self, project: Project) -> None:
        """Insert or update a project, keyed by ``project.id`` (idempotent)."""
        ...

    async def list_all(self) -> list[Project]:
        """Every registered project, ordered by name."""
        ...

    async def get(self, project_id: str) -> Project | None:
        """One project by id, or ``None`` if it isn't registered."""
        ...

    async def check(self) -> None:
        """Raise if the registry can't be reached."""
        ...

    async def close(self) -> None: ...
