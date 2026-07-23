from types import SimpleNamespace

import pytest

from entities.commit import CommitInfo
from shared.common.indexing import temporal

PROJECT = "proj_aaaa1111"


def _node(node_id, qualified_name, kind="function", metadata=None):
    return SimpleNamespace(
        id=node_id,
        qualified_name=qualified_name,
        name=qualified_name.rsplit(".", 1)[-1],
        kind=SimpleNamespace(value=kind),
        file_path="m.py",
        span=None,
        metadata=metadata or {},
    )


def _commit(sha, time_update=100):
    return CommitInfo(ref="main", sha=sha, time_update=time_update)


async def _state_ids(store, at_seq):
    rows = await temporal.state_at(store, PROJECT, "main", at_seq)
    return {row["node_id"] for row in rows}


@pytest.mark.integration
@pytest.mark.temporal
async def test_state_at_reflects_each_commit_point_in_time(graph_store):
    # Arrange — commit s1 (indexed first, seq 1) creates a & b;
    # commit s2 (seq 2) drops a, adds c.
    await temporal.ensure_temporal_schema(graph_store)
    await temporal.append_versions(
        graph_store, ".", PROJECT, _commit("s1"),
        [_node("a", "mod.a"), _node("b", "mod.b")],
    )
    await temporal.append_versions(
        graph_store, ".", PROJECT, _commit("s2"),
        [_node("b", "mod.b"), _node("c", "mod.c")],
    )

    # Act
    at_one = await _state_ids(graph_store, 1)
    at_two = await _state_ids(graph_store, 2)

    # Assert — the past still sees a; the present sees the delete and the add.
    assert at_one == {"a", "b"}
    assert at_two == {"b", "c"}


@pytest.mark.integration
@pytest.mark.temporal
async def test_reindexing_the_same_commit_is_a_no_op(graph_store):
    # Arrange
    await temporal.ensure_temporal_schema(graph_store)
    nodes = [_node("a", "mod.a"), _node("b", "mod.b")]
    await temporal.append_versions(
        graph_store, ".", PROJECT, _commit("s1"), nodes,
    )

    # Act — re-run the identical commit (same sha).
    counts = await temporal.append_versions(
        graph_store, ".", PROJECT, _commit("s1"), nodes,
    )

    # Assert — no new versions; it resolves to the commit's original seq.
    assert counts["created"] == 0
    assert counts["updated"] == 0
    assert counts["deleted"] == 0
    assert counts["seq"] == 1
    assert await _state_ids(graph_store, 1) == {"a", "b"}


@pytest.mark.integration
@pytest.mark.temporal
async def test_out_of_order_indexing_advances_head_monotonically(graph_store):
    # Regression (P02 review): seq is assigned as index-order, so a later index
    # of a different commit never rewinds the head or corrupts an earlier
    # point-in-time — even when the later run carries "more" nodes.
    await temporal.ensure_temporal_schema(graph_store)

    # Arrange / Act — first run sees {b, c}; a later run indexes {a, b, c}.
    first = await temporal.append_versions(
        graph_store, ".", PROJECT, _commit("newer"),
        [_node("b", "mod.b"), _node("c", "mod.c")],
    )
    second = await temporal.append_versions(
        graph_store, ".", PROJECT, _commit("older"),
        [_node("a", "mod.a"), _node("b", "mod.b"), _node("c", "mod.c")],
    )

    # Assert — seq only advances, and each point-in-time stays intact.
    assert first["seq"] == 1
    assert second["seq"] == 2
    assert await _state_ids(graph_store, 1) == {"b", "c"}
    assert await _state_ids(graph_store, 2) == {"a", "b", "c"}


@pytest.mark.integration
@pytest.mark.temporal
async def test_content_change_is_recorded_as_an_update(graph_store):
    # Arrange — same node id, different metadata across two commits.
    await temporal.ensure_temporal_schema(graph_store)
    await temporal.append_versions(
        graph_store, ".", PROJECT, _commit("s1"),
        [_node("a", "mod.a", metadata={"sig": "() -> int"})],
    )

    # Act
    counts = await temporal.append_versions(
        graph_store, ".", PROJECT, _commit("s2"),
        [_node("a", "mod.a", metadata={"sig": "() -> str"})],
    )

    # Assert
    assert counts["updated"] == 1
    assert counts["created"] == 0
