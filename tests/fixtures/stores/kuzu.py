from collections.abc import AsyncGenerator
from pathlib import Path

import pytest_asyncio

from shared.common.db.graph.kuzu import KuzuGraphStore
from shared.common.db.registry.kuzu import KuzuProjectRegistry
from shared.common.indexing import persist


@pytest_asyncio.fixture
async def graph_store(tmp_path: Path) -> AsyncGenerator[KuzuGraphStore]:
    """An embedded Kuzu code-graph store with the code schema, per test.

    Each test gets its own on-disk database under ``tmp_path`` (so runs are
    isolated and xdist-safe) and the connection is closed afterwards.
    """
    store = KuzuGraphStore.open(tmp_path / "graph")
    await persist.ensure_code_schema(store)
    try:
        yield store
    finally:
        await store.close()


@pytest_asyncio.fixture
async def registry(
    tmp_path: Path,
) -> AsyncGenerator[KuzuProjectRegistry]:
    """An embedded Kuzu project registry with its schema, per test."""
    store = KuzuProjectRegistry.open(tmp_path / "registry")
    await store.ensure_schema()
    try:
        yield store
    finally:
        await store.close()
