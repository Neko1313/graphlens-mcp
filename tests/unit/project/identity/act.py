import re

import pytest

from features.projects.service import (
    build_project,
    compute_project_id,
    normalize_remote,
    normalize_subpath,
)

CANONICAL = "github.com/neko1313/graphlens-mcp"


@pytest.mark.unit
@pytest.mark.project
@pytest.mark.parametrize(
    "remote_url",
    [
        "https://github.com/Neko1313/graphlens-mcp.git",
        "https://github.com/Neko1313/graphlens-mcp",
        "git@github.com:Neko1313/graphlens-mcp.git",
        "ssh://git@github.com/Neko1313/graphlens-mcp.git",
        "https://user:token@github.com:443/Neko1313/graphlens-mcp.git/",
        "https://GITHUB.com/Neko1313/GraphLens-MCP.git",
    ],
)
def test_every_remote_spelling_canonicalizes_the_same(remote_url):
    # Arrange / Act
    canonical = normalize_remote(remote_url)

    # Assert
    assert canonical == CANONICAL


@pytest.mark.unit
@pytest.mark.project
def test_ssh_and_https_clones_share_one_project_id():
    # Arrange
    ssh = "git@github.com:Neko1313/graphlens-mcp.git"
    https = "https://github.com/Neko1313/graphlens-mcp.git"

    # Act
    ssh_id = compute_project_id(ssh)
    https_id = compute_project_id(https)

    # Assert
    assert ssh_id == https_id


@pytest.mark.unit
@pytest.mark.project
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ""),
        (".", ""),
        ("/", ""),
        ("/src/api/", "src/api"),
        ("src/./api", "src/api"),
        ("services\\api", "services/api"),
    ],
)
def test_subpath_is_normalized_to_clean_posix(raw, expected):
    # Arrange / Act
    normalized = normalize_subpath(raw)

    # Assert
    assert normalized == expected


@pytest.mark.unit
@pytest.mark.project
def test_distinct_subpaths_of_one_repo_are_distinct_projects():
    # Arrange
    remote = "https://github.com/o/mono.git"

    # Act
    whole = compute_project_id(remote)
    pkg_a = compute_project_id(remote, "packages/a")
    pkg_b = compute_project_id(remote, "packages/b")

    # Assert
    assert len({whole, pkg_a, pkg_b}) == 3


@pytest.mark.unit
@pytest.mark.project
def test_empty_and_dot_subpath_both_mean_the_whole_repo():
    # Arrange
    remote = "https://github.com/o/r.git"

    # Act / Assert
    assert compute_project_id(remote, "") == compute_project_id(remote, ".")


@pytest.mark.unit
@pytest.mark.project
@pytest.mark.parametrize(
    ("remote", "subpath"),
    [
        ("git@github.com:Weird.Org/My_Repo.git", "a/b-c"),
        ("https://example.com/x", ""),
        ("ssh://git@host:22/deep/nested/repo.git", "svc/api"),
    ],
)
def test_project_id_stays_filter_safe(remote, subpath):
    # A project id is inlined into a Milvus filter and used as a graph value,
    # so it must be [A-Za-z0-9_] and start with a letter/underscore.
    # Arrange / Act
    project_id = compute_project_id(remote, subpath)

    # Assert
    assert re.fullmatch(r"[A-Za-z0-9_]+", project_id)
    assert project_id[0].isalpha() or project_id[0] == "_"


@pytest.mark.unit
@pytest.mark.project
def test_build_project_identity_ignores_the_checkout_path(
    monkeypatch, tmp_path,
):
    # Two clones of one repo at different on-disk paths are one project.
    # Arrange
    monkeypatch.setattr(
        "features.projects.service.get_remote_url",
        lambda _root: "git@github.com:o/r.git",
    )
    (tmp_path / "clone-a").mkdir()
    (tmp_path / "clone-b").mkdir()

    # Act
    from_a = build_project(tmp_path / "clone-a")
    from_b = build_project(tmp_path / "clone-b")

    # Assert
    assert from_a.id == from_b.id
    assert from_a.subpath == ""
    assert from_a.git_url == "git@github.com:o/r.git"


@pytest.mark.unit
@pytest.mark.project
def test_build_project_carries_normalized_subpath(monkeypatch, tmp_path):
    # Arrange
    monkeypatch.setattr(
        "features.projects.service.get_remote_url",
        lambda _root: "https://github.com/o/mono.git",
    )

    # Act
    project = build_project(tmp_path, subpath="/services/api/")

    # Assert
    assert project.subpath == "services/api"
    assert project.id == compute_project_id(
        "https://github.com/o/mono.git", "services/api",
    )
