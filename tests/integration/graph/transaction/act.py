import asyncio

import pytest

from shared.common.indexing import persist


async def _count(store, project_id: str) -> int:
    rows = await store.execute(
        "MATCH (n:CodeNode {project_id: $p}) RETURN count(n) AS c",
        {"p": project_id},
    )
    return rows[0]["c"]


@pytest.mark.integration
@pytest.mark.indexing
async def test_a_committed_transaction_applies_every_statement(graph_store):
    # Arrange / Act
    async with graph_store.transaction() as tx:
        for local_id in ("a", "b"):
            await tx.execute(
                "CREATE (n:CodeNode {id: $id, project_id: $p, local_id: $id})",
                {"id": local_id, "p": "tx_ok"},
            )

    # Assert
    assert await _count(graph_store, "tx_ok") == 2


@pytest.mark.integration
@pytest.mark.indexing
async def test_a_transaction_reads_its_own_uncommitted_writes(graph_store):
    # Query helpers take a GraphExecutor precisely so a read inside the
    # transaction sees what the transaction has written so far.
    # Arrange / Act
    async with graph_store.transaction() as tx:
        await tx.execute(
            "CREATE (n:CodeNode {id: $id, project_id: $p, local_id: 'x'})",
            {"id": "tx_self::x", "p": "tx_self"},
        )
        inside = await _count(tx, "tx_self")

    # Assert
    assert inside == 1
    assert await _count(graph_store, "tx_self") == 1


@pytest.mark.integration
@pytest.mark.indexing
async def test_ordinary_statements_wait_out_an_open_transaction(graph_store):
    # Kuzu permits one write transaction system-wide: an ordinary statement
    # issued while a transaction is open would fail outright without the gate,
    # so it must be held back and then succeed.
    # Arrange
    async def concurrent_write() -> None:
        await persist.set_graph_head(graph_store, "tx_gate", "sha")

    # Act
    async with graph_store.transaction() as tx:
        await tx.execute(
            "CREATE (n:CodeNode {id: $id, project_id: $p, local_id: 'y'})",
            {"id": "tx_gate::y", "p": "tx_gate"},
        )
        task = asyncio.create_task(concurrent_write())
        await asyncio.sleep(0)
        blocked_while_open = not task.done()
    await task

    # Assert
    assert blocked_while_open
    assert await persist.graph_head(graph_store, "tx_gate") == "sha"
    assert await _count(graph_store, "tx_gate") == 1
