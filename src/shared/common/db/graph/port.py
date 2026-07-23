from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol

__all__ = ["GraphExecutor", "GraphStore", "GraphStoreError"]


class GraphStoreError(Exception):
    """A Cypher query failed — wraps the backend-native driver exception.

    Callers written against ``GraphExecutor``/``GraphStore`` catch this one
    type regardless of which backend (Kuzu or Neo4j) is behind the port,
    instead of importing driver-specific exception hierarchies.
    """


class GraphExecutor(Protocol):
    """Something that runs Cypher — the store itself, or one of its
    transactions.

    Query helpers take THIS, not ``GraphStore``, so the same function composes
    inside and outside a transaction (a read done inside one then sees that
    transaction's uncommitted writes).
    """

    async def execute(
        self,
        query: str,
        parameters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Run a Cypher query, returning rows as column-name-keyed dicts."""
        ...


class GraphStore(GraphExecutor, Protocol):
    """A Cypher-speaking graph store — Kuzu (embedded) or Neo4j (host).

    Both backends run the same Cypher, so callers write one query and
    never import kuzu/neo4j directly — only this port.
    """

    def transaction(self) -> AbstractAsyncContextManager[GraphExecutor]:
        """A write transaction: all-or-nothing across several statements.

        Statements run on the yielded executor commit together when the block
        exits cleanly, and are discarded if it raises or is cancelled. Use it
        wherever a half-applied write would be *wrong* rather than merely
        stale — the append-only log especially, where orphaned rows at a seq
        would corrupt every later time-travel read.
        """
        ...

    async def check(self) -> None:
        """Raise if the store can't be reached or queried.

        For lifespan startup: local file opened / remote host reachable.
        """
        ...

    async def close(self) -> None: ...
