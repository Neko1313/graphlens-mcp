import pytest

from entities.commit import CommitInfo
from shared.common.indexing import temporal

PROJECT = "proj_reads_err"


@pytest.mark.integration
@pytest.mark.temporal
async def test_a_project_with_no_history_resolves_to_nothing(graph_store):
    # Arrange / Act
    await temporal.ensure_temporal_schema(graph_store)
    point = await temporal.resolve_point(graph_store, PROJECT)

    # Assert
    assert point is None


@pytest.mark.integration
@pytest.mark.temporal
async def test_an_unknown_ref_or_commit_resolves_to_nothing(graph_store):
    # Arrange
    await temporal.ensure_temporal_schema(graph_store)
    await temporal.append_versions(
        graph_store,
        ".",
        PROJECT,
        CommitInfo(ref="main", sha="a" * 40, time_update=1),
        [],
    )

    # Act / Assert — a ref never indexed, and a sha never seen.
    assert await temporal.resolve_point(graph_store, PROJECT, "nope") is None
    assert (
        await temporal.resolve_point(
            graph_store,
            PROJECT,
            "main",
            "deadbeef",
        )
        is None
    )


@pytest.mark.integration
@pytest.mark.temporal
async def test_an_all_digit_sha_prefix_falls_back_to_the_sha(graph_store):
    # A sha is hex, so a prefix can be all digits and collide with the seq
    # reading. Seq wins when one matches; otherwise the value is a sha.
    # Arrange
    await temporal.ensure_temporal_schema(graph_store)
    sha = "9" * 40
    await temporal.append_versions(
        graph_store,
        ".",
        PROJECT,
        CommitInfo(ref="digits", sha=sha, time_update=1),
        [],
    )

    # Act — "999999" is no seq, so it must resolve as a sha prefix.
    point = await temporal.resolve_point(
        graph_store,
        PROJECT,
        "digits",
        "999999",
    )

    # Assert
    assert point is not None
    assert point.sha == sha


@pytest.mark.integration
@pytest.mark.temporal
async def test_an_ambiguous_sha_prefix_resolves_to_nothing(graph_store):
    # Two commits sharing a prefix must not silently pick one — an arbitrary
    # answer to "as of ab…" is worse than no answer.
    # Arrange
    await temporal.ensure_temporal_schema(graph_store)
    for sha in ("abc111", "abc222"):
        await temporal.append_versions(
            graph_store,
            ".",
            PROJECT,
            CommitInfo(ref="dup", sha=sha, time_update=1),
            [],
        )

    # Act
    point = await temporal.resolve_point(graph_store, PROJECT, "dup", "abc")

    # Assert
    assert point is None
