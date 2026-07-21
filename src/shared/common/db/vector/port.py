from typing import Any, Protocol

__all__ = ["VectorStore"]


class VectorStore(Protocol):
    """An embeddings store — Milvus Lite (embedded) or full Milvus (host).

    Both run through the same client, one collection per project. Rows and
    hits are plain dicts (id, vector/score, payload) — never a driver type.
    """

    async def ensure_collection(self, collection: str, dim: int) -> None:
        """Create the collection if it doesn't exist yet."""
        ...

    async def upsert(
        self,
        collection: str,
        rows: list[dict[str, Any]],
    ) -> None:
        """Insert or update rows (each with an "id" and a "vector" key)."""
        ...

    async def search(
        self,
        collection: str,
        vector: list[float],
        limit: int = 10,
        filter_expr: str | None = None,
        output_fields: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Nearest-neighbour search, optionally narrowed by a filter expr.

        `output_fields` must name payload fields explicitly — Milvus's `"*"`
        wildcard does not reliably expand dynamic fields.
        """
        ...

    async def delete(self, collection: str, filter_expr: str) -> None:
        """Delete every row matching the filter expression."""
        ...

    async def drop_collection(self, collection: str) -> None:
        """Drop a whole collection (no-op if it doesn't exist)."""
        ...

    async def check(self) -> None:
        """Raise if the store can't be reached.

        For lifespan startup: local file opened / remote host reachable.
        Reachability only — a specific project's collection existing is
        verified separately via `ensure_collection`.
        """
        ...

    async def close(self) -> None: ...
