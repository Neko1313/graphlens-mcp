import pytest

from features.projects import git, service
from shared.common.context import AppContext


@pytest.fixture(autouse=True)
def _isolated_checkout_root(tmp_path, monkeypatch):
    monkeypatch.setattr(git, "_CHECKOUT_ROOT", tmp_path / "checkouts")


@pytest.mark.integration
@pytest.mark.project
async def test_index_remote_retains_a_readable_checkout(
    graph_store,
    vector_store,
    registry,
    git_repo,
):
    # Regression (final review): index_remote used to delete the checkout it
    # registered as project.path, breaking every disk-backed read in server
    # mode. The tree must survive so reads work.
    # Arrange
    app = AppContext(graph_store, vector_store, registry)

    # Act
    project, result = await service.index_remote(
        str(git_repo),
        "main",
        None,
        app,
    )

    # Assert — the working tree the project points at still exists on disk.
    assert result.nodes > 0
    assert project.path.exists()
    assert (project.path / "m.py").exists()
