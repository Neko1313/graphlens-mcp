import pytest

from entities.commit import CommitInfo
from shared.common.indexing import index_project_graph, persist

PROJECT = "proj_head"


def _project(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='p'\nversion='0'\n")
    pkg = root / "p"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "m.py").write_text("def foo():\n    return 1\n")
    return root


@pytest.mark.integration
@pytest.mark.indexing
async def test_graph_head_tracks_the_last_indexed_commit(
    tmp_path,
    graph_store,
    vector_store,
):
    # Regression (P03 review): the no-op fast path must gate on the sha the
    # single-snapshot graph CURRENTLY holds, not "any ref ever indexed" — so
    # graph_head follows the last index, even across different refs.
    # Arrange
    root = _project(tmp_path)
    sha_a, sha_b = "a" * 40, "b" * 40

    # Act — index ref main@A, then ref release@B into the SAME project.
    await index_project_graph(
        root,
        PROJECT,
        graph_store,
        vector_store,
        None,
        CommitInfo(ref="main", sha=sha_a, time_update=1),
    )
    head_after_a = await persist.graph_head(graph_store, PROJECT)
    await index_project_graph(
        root,
        PROJECT,
        graph_store,
        vector_store,
        None,
        CommitInfo(ref="release", sha=sha_b, time_update=2),
    )
    head_after_b = await persist.graph_head(graph_store, PROJECT)

    # Assert — the graph reflects the last index; a stale main@A is NOT current.
    assert head_after_a == sha_a
    assert head_after_b == sha_b
