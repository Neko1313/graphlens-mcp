from types import SimpleNamespace

import pytest

from entities.commit import CommitInfo
from features.info import service as info_service
from features.relations import service as relations_service
from shared.common.indexing import temporal

PROJECT = "proj_reads"
SHA_ONE, SHA_TWO = "1ab" * 13 + "c", "2de" * 13 + "f"


def _node(node_id, name=None, kind="function"):
    return SimpleNamespace(
        id=node_id,
        qualified_name=f"mod.{name or node_id}",
        name=name or node_id,
        kind=SimpleNamespace(value=kind),
        file_path="m.py",
        span=None,
        metadata={},
    )


def _rel(source, target, kind="calls"):
    return SimpleNamespace(
        source_id=source,
        target_id=target,
        kind=SimpleNamespace(value=kind),
        metadata={},
    )


@pytest.fixture
async def history(graph_store):
    """Two commits: seq 1 has a→b, seq 2 replaces b with c and drops a→b."""
    await temporal.ensure_temporal_schema(graph_store)
    await temporal.append_versions(
        graph_store, ".", PROJECT,
        CommitInfo(ref="main", sha=SHA_ONE, time_update=111),
        [_node("a", "alpha"), _node("b", "beta")], [_rel("a", "b")],
    )
    await temporal.append_versions(
        graph_store, ".", PROJECT,
        CommitInfo(ref="main", sha=SHA_TWO, time_update=222),
        [_node("a", "alpha"), _node("c", "gamma")], [_rel("a", "c")],
    )
    return graph_store


@pytest.mark.integration
@pytest.mark.temporal
async def test_a_point_resolves_from_a_sha_prefix(history):
    # Arrange / Act
    point = await temporal.resolve_point(history, PROJECT, "main", SHA_ONE[:7])

    # Assert
    assert point is not None
    assert point.seq == 1
    assert point.sha == SHA_ONE
    assert point.time_update == 111
    assert not point.is_head


@pytest.mark.integration
@pytest.mark.temporal
async def test_an_empty_at_resolves_to_the_ref_head(history):
    # Arrange / Act
    point = await temporal.resolve_point(history, PROJECT, "main")

    # Assert
    assert point is not None
    assert point.seq == 2
    assert point.is_head


@pytest.mark.integration
@pytest.mark.relations
async def test_relations_report_the_neighbours_of_that_commit(history):
    # Arrange
    old = await temporal.resolve_point(history, PROJECT, "main", "1")
    new = await temporal.resolve_point(history, PROJECT, "main", "2")
    assert old is not None
    assert new is not None

    # Act
    then = await relations_service.get_relations_at(
        history, PROJECT, old, "a",
    )
    now = await relations_service.get_relations_at(
        history, PROJECT, new, "a",
    )

    # Assert — the call that was replaced is still there in the past.
    assert then is not None
    assert now is not None
    assert {ref.id for ref in then.callees} == {"b"}
    assert {ref.id for ref in now.callees} == {"c"}
    assert then.revision is not None
    assert then.revision.sha == SHA_ONE


@pytest.mark.integration
@pytest.mark.info
async def test_info_returns_the_shape_recorded_at_that_commit(history):
    # Arrange
    point = await temporal.resolve_point(history, PROJECT, "main", "1")
    assert point is not None

    # Act — 'beta' no longer exists at head, but did at seq 1.
    found = await info_service.info_at(history, PROJECT, point, "beta")

    # Assert
    assert found is not None
    assert found.type == "node"
    assert found.id == "b"
    assert found.qualified_name == "mod.beta"
    # Source text is not versioned, and the result says so rather than
    # silently returning today's body for yesterday's symbol.
    assert found.source == ""
    assert found.revision is not None
    assert "not stored per revision" in found.revision.note


@pytest.mark.integration
@pytest.mark.info
async def test_a_file_outline_lists_the_symbols_of_that_commit(history):
    # Arrange
    point = await temporal.resolve_point(history, PROJECT, "main", "1")
    assert point is not None

    # Act
    outline = await info_service.info_at(history, PROJECT, point, "m.py")

    # Assert
    assert outline is not None
    assert outline.type == "outline"
    assert {entry.id for entry in outline.symbols} == {"a", "b"}
