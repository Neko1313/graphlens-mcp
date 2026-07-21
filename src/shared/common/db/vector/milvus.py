import asyncio
from typing import Any

from pymilvus import MilvusClient

__all__ = ["MilvusVectorStore"]


class MilvusVectorStore:
    """Milvus backend — same client for Milvus Lite (embedded) and a
    full/host Milvus, only the connection URI differs.

    The async client's Lite support doesn't hold up under an index build
    (grpc "Method not implemented!" on the local-server path), so this
    wraps the sync `MilvusClient` in a thread instead — the same trick
    kuzu's own `AsyncConnection` uses internally.
    """

    def __init__(self, client: MilvusClient) -> None:
        self._client = client
        self._loaded: set[str] = set()

    @classmethod
    def open(cls, uri: str) -> "MilvusVectorStore":
        return cls(MilvusClient(uri=uri))

    async def _load(self, collection: str) -> None:
        await asyncio.to_thread(self._client.load_collection, collection)
        self._loaded.add(collection)

    async def _ensure_loaded(self, collection: str) -> bool:
        """Load the collection into memory once per process; False if absent.

        Milvus Lite reopens an existing on-disk collection in the "released"
        state, so search/query must load it first — the indexing path does
        this via ensure_collection, but read paths reach a collection this
        process never created (e.g. after a server restart).
        """
        if collection in self._loaded:
            return True
        exists = await asyncio.to_thread(
            self._client.has_collection, collection,
        )
        if not exists:
            return False
        await self._load(collection)
        return True

    async def ensure_collection(self, collection: str, dim: int) -> None:
        exists = await asyncio.to_thread(
            self._client.has_collection, collection
        )
        if not exists:
            await asyncio.to_thread(
                self._client.create_collection,
                collection,
                dimension=dim,
                primary_field_name="id",
                id_type="string",
                max_length=512,
                vector_field_name="vector",
                metric_type="COSINE",
            )
        # A collection opened from an existing local DB file starts out
        # "released" — only a just-created one is auto-loaded. Idempotent,
        # so this is safe to call every time regardless of which branch ran.
        await self._load(collection)

    async def upsert(
        self,
        collection: str,
        rows: list[dict[str, Any]],
    ) -> None:
        await asyncio.to_thread(self._client.upsert, collection, rows)

    async def search(
        self,
        collection: str,
        vector: list[float],
        limit: int = 10,
        filter_expr: str | None = None,
        output_fields: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        if not await self._ensure_loaded(collection):
            return []
        hits = await asyncio.to_thread(
            self._client.search,
            collection,
            data=[vector],
            limit=limit,
            filter=filter_expr or "",
            output_fields=output_fields,
        )
        return list(hits[0]) if hits else []

    async def delete(self, collection: str, filter_expr: str) -> None:
        await asyncio.to_thread(
            self._client.delete, collection, filter=filter_expr
        )

    async def check(self) -> None:
        await asyncio.to_thread(self._client.list_collections)

    async def close(self) -> None:
        await asyncio.to_thread(self._client.close)
