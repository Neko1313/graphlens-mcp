from typing import Any, Protocol

__all__ = ["GraphStore"]


class GraphStore(Protocol):
    """A Cypher-speaking graph store — Kuzu (embedded) or Neo4j (host).

    Both backends run the same Cypher, so callers write one query and
    never import kuzu/neo4j directly — only this port.
    """

    async def execute(
        self,
        query: str,
        parameters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Run a Cypher query, returning rows as column-name-keyed dicts."""
        ...

    async def check(self) -> None:
        """Raise if the store can't be reached or queried.

        For lifespan startup: local file opened / remote host reachable.
        """
        ...

    async def close(self) -> None: ...
