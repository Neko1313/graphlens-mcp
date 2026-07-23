from pathlib import Path

from entities.project import Project
from shared.common.db.graph.kuzu import KuzuGraphStore

__all__ = ["KuzuProjectRegistry"]

_FIELDS = (
    "p.id AS id, p.name AS name, p.path AS path, "
    "p.description AS description, p.git_url AS git_url"
)


class KuzuProjectRegistry:
    """Registry backed by a dedicated embedded Kuzu database.

    Reuses ``KuzuGraphStore`` for the async, thread-wrapped Cypher execution;
    stores each project as a single ``Project`` node.
    """

    def __init__(self, store: KuzuGraphStore) -> None:
        self._store = store

    @classmethod
    def open(cls, path: Path) -> "KuzuProjectRegistry":
        return cls(KuzuGraphStore.open(path))

    async def ensure_schema(self) -> None:
        await self._store.execute(
            "CREATE NODE TABLE IF NOT EXISTS Project("
            "id STRING, name STRING, path STRING, "
            "description STRING, git_url STRING, "
            "PRIMARY KEY(id))",
        )

    async def add(self, project: Project) -> None:
        await self._store.execute(
            "MERGE (p:Project {id: $id}) "
            "SET p.name = $name, p.path = $path, "
            "p.description = $description, p.git_url = $git_url",
            {
                "id": project.id,
                "name": project.name,
                "path": str(project.path),
                "description": project.description,
                "git_url": project.git_url,
            },
        )

    async def list_all(self) -> list[Project]:
        rows = await self._store.execute(
            f"MATCH (p:Project) RETURN {_FIELDS} ORDER BY p.name",
        )
        return [Project.model_validate(row) for row in rows]

    async def get(self, project_id: str) -> Project | None:
        rows = await self._store.execute(
            f"MATCH (p:Project {{id: $id}}) RETURN {_FIELDS}",
            {"id": project_id},
        )
        return Project.model_validate(rows[0]) if rows else None

    async def remove(self, project_id: str) -> None:
        await self._store.execute(
            "MATCH (p:Project {id: $id}) DELETE p",
            {"id": project_id},
        )

    async def check(self) -> None:
        await self._store.check()

    async def close(self) -> None:
        await self._store.close()
