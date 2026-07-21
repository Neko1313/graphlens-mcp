from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp.server import MCPServer

from shared.common.context import AppContext
from shared.common.db.graph import get_graph_store
from shared.common.db.registry import get_registry_store
from shared.common.db.vector import get_vector_store

__all__ = ["lifespan"]


@asynccontextmanager
async def lifespan(_server: MCPServer) -> AsyncIterator[AppContext]:
    """Resolve the configured stores and verify each is reachable before
    serving a single request — a misconfigured DSN or an unreachable host
    fails startup instead of the first tool call. The registry's schema is
    ensured here too, so tools never race to create it.
    """
    graph_store = get_graph_store()
    vector_store = get_vector_store()
    registry = get_registry_store()

    await graph_store.check()
    await vector_store.check()
    await registry.check()
    await registry.ensure_schema()

    try:
        yield AppContext(
            graph_store=graph_store,
            vector_store=vector_store,
            registry=registry,
        )
    finally:
        await graph_store.close()
        await vector_store.close()
        await registry.close()
