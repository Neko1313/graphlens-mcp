from types import SimpleNamespace

import pytest

from entities.commit import CommitInfo
from shared.common.indexing import temporal

PROJECT = "proj_edges"


def _node(node_id):
    return SimpleNamespace(
        id=node_id,
        qualified_name=f"mod.{node_id}",
        name=node_id,
        kind=SimpleNamespace(value="function"),
        file_path="m.py",
        span=None,
        metadata={},
    )


def _rel(source, target, kind="calls", metadata=None):
    return SimpleNamespace(
        source_id=source,
        target_id=target,
        kind=SimpleNamespace(value=kind),
        metadata=metadata or {},
    )


def _commit(sha, time_update=100):
    return CommitInfo(ref="main", sha=sha, time_update=time_update)


async def _callees(store, node_id, at_seq):
    rows = await temporal.edges_at(
        store,
        PROJECT,
        "main",
        at_seq,
        [node_id],
        "out",
        "calls",
    )
    return {row["target_id"] for row in rows}


@pytest.mark.integration
@pytest.mark.temporal
async def test_each_point_in_time_keeps_its_own_edges(graph_store):
    # The reason edges are versioned at all: a past point must answer "what
    # called what THEN", not replay today's call graph.
    # Arrange — seq 1: a→b. seq 2: a→b dropped, a→c added.
    await temporal.ensure_temporal_schema(graph_store)
    await temporal.append_versions(
        graph_store,
        ".",
        PROJECT,
        _commit("s1"),
        [_node("a"), _node("b"), _node("c")],
        [_rel("a", "b")],
    )
    await temporal.append_versions(
        graph_store,
        ".",
        PROJECT,
        _commit("s2"),
        [_node("a"), _node("b"), _node("c")],
        [_rel("a", "c")],
    )

    # Act
    at_one = await _callees(graph_store, "a", 1)
    at_two = await _callees(graph_store, "a", 2)

    # Assert
    assert at_one == {"b"}
    assert at_two == {"c"}


@pytest.mark.integration
@pytest.mark.temporal
async def test_a_deleted_edge_stays_deleted_at_later_points(graph_store):
    # A tombstone must mask the create it supersedes at EVERY later seq —
    # otherwise a removed edge reappears the moment another commit lands.
    # Arrange
    await temporal.ensure_temporal_schema(graph_store)
    nodes = [_node("a"), _node("b")]
    await temporal.append_versions(
        graph_store,
        ".",
        PROJECT,
        _commit("s1"),
        nodes,
        [_rel("a", "b")],
    )
    await temporal.append_versions(
        graph_store,
        ".",
        PROJECT,
        _commit("s2"),
        nodes,
        [],
    )

    # Act — a third commit that touches nothing.
    await temporal.append_versions(
        graph_store,
        ".",
        PROJECT,
        _commit("s3"),
        nodes,
        [],
    )

    # Assert
    assert await _callees(graph_store, "a", 1) == {"b"}
    assert await _callees(graph_store, "a", 2) == set()
    assert await _callees(graph_store, "a", 3) == set()


@pytest.mark.integration
@pytest.mark.temporal
async def test_only_changed_edges_are_appended(graph_store):
    # Arrange
    await temporal.ensure_temporal_schema(graph_store)
    nodes = [_node("a"), _node("b"), _node("c")]
    edges = [_rel("a", "b"), _rel("b", "c")]
    await temporal.append_versions(
        graph_store,
        ".",
        PROJECT,
        _commit("s1"),
        nodes,
        edges,
    )

    # Act — one edge unchanged, one gone, one new.
    counts = await temporal.append_versions(
        graph_store,
        ".",
        PROJECT,
        _commit("s2"),
        nodes,
        [_rel("a", "b"), _rel("a", "c")],
    )

    # Assert
    assert counts["edge_created"] == 1
    assert counts["edge_deleted"] == 1
    assert counts["edge_updated"] == 0


@pytest.mark.integration
@pytest.mark.temporal
async def test_edges_to_unknown_nodes_are_not_logged(graph_store):
    # Mirrors persist_relations: an edge whose endpoint was never stored would
    # make the log claim neighbours the graph can't name.
    # Arrange / Act
    await temporal.ensure_temporal_schema(graph_store)
    counts = await temporal.append_versions(
        graph_store,
        ".",
        PROJECT,
        _commit("s1"),
        [_node("a")],
        [_rel("a", "ghost")],
    )

    # Assert
    assert counts["edge_created"] == 0
    assert await _callees(graph_store, "a", 1) == set()
