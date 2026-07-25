from types import SimpleNamespace

import pytest

from entities.commit import CommitInfo
from shared.common.indexing import temporal


def _node(node_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=node_id,
        qualified_name=f"mod.{node_id}",
        name=node_id,
        kind=SimpleNamespace(value="function"),
        file_path="m.py",
        span=None,
        metadata={},
    )


class _Boom(RuntimeError):
    """Raised inside a transaction block to abort it."""


@pytest.mark.integration
@pytest.mark.indexing
async def test_a_failed_transaction_discards_every_statement(graph_store):
    # Arrange / Act
    with pytest.raises(_Boom):
        async with graph_store.transaction() as tx:
            await tx.execute(
                "CREATE (n:CodeNode {id: 'tx_bad::a', project_id: 'tx_bad', "
                "local_id: 'a'})",
            )
            raise _Boom

    # Assert
    rows = await graph_store.execute(
        "MATCH (n:CodeNode {project_id: 'tx_bad'}) RETURN count(n) AS c",
    )
    assert rows[0]["c"] == 0


@pytest.mark.integration
@pytest.mark.indexing
async def test_the_store_still_works_after_a_failed_transaction(graph_store):
    # The dedicated connection is closed on the failure path; the pooled one
    # the store uses for everything else must be unaffected.
    # Arrange
    with pytest.raises(_Boom):
        async with graph_store.transaction() as tx:
            await tx.execute("MATCH (n:CodeNode) RETURN n LIMIT 1")
            raise _Boom

    # Act
    async with graph_store.transaction() as tx:
        await tx.execute(
            "CREATE (n:CodeNode {id: 'tx_after::a', project_id: 'tx_after', "
            "local_id: 'a'})",
        )
    rows = await graph_store.execute(
        "MATCH (n:CodeNode {project_id: 'tx_after'}) RETURN count(n) AS c",
    )

    # Assert
    assert rows[0]["c"] == 1


@pytest.mark.integration
@pytest.mark.temporal
async def test_an_aborted_append_strands_no_version_rows(
    graph_store,
    monkeypatch,
    tmp_path,
):
    # Regression (final review, #3): the append used to write NodeVersion rows,
    # RefCommit, and RefState as separate autocommits, so a crash between them
    # left rows at a seq the head never reached — invisible to every read, yet
    # blocking that seq for the next commit. It is one transaction now.
    # Arrange
    await temporal.ensure_temporal_schema(graph_store)
    project, commit = (
        "tx_temporal",
        CommitInfo(
            ref="main",
            sha="a" * 40,
            time_update=1,
        ),
    )
    original = temporal._advance_head

    async def explode(*args, **kwargs):
        await original(*args, **kwargs)
        msg = "crash after the head records"
        raise _Boom(msg)

    monkeypatch.setattr(temporal, "_advance_head", explode)

    # Act
    with pytest.raises(_Boom):
        await temporal.append_versions(
            graph_store,
            tmp_path,
            project,
            commit,
            [_node("a"), _node("b")],
        )

    # Assert — nothing at all landed: no version rows, no head, no commit map.
    for label in ("NodeVersion", "RefState", "RefCommit"):
        rows = await graph_store.execute(
            f"MATCH (v:{label} {{project_id: $p}}) RETURN count(v) AS c",
            {"p": project},
        )
        assert rows[0]["c"] == 0
