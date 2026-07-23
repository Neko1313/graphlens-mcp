import pytest

from features.projects.service import NoRemoteError, build_project


@pytest.mark.unit
@pytest.mark.project
def test_build_project_without_a_remote_raises(monkeypatch, tmp_path):
    # Arrange — a directory whose repo has no remote (or is not a repo).
    monkeypatch.setattr(
        "features.projects.service.get_remote_url", lambda _root: None,
    )

    # Act / Assert
    with pytest.raises(NoRemoteError):
        build_project(tmp_path)
