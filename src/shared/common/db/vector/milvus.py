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

    @classmethod
    def open(cls, uri: str) -> "MilvusVectorStore":
        return cls(MilvusClient(uri=uri))

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
        await asyncio.to_thread(self._client.load_collection, collection)

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
