import pytest

from features.projects import git


@pytest.mark.integration
@pytest.mark.project
def test_retain_checkout_rejects_a_path_traversal_project_id(
    git_repo, tmp_path
):
    # project_id is always compute_project_id()'s own charset in practice,
    # but retain_checkout/remove_retained build a filesystem path from it
    # directly — a "../"-laced id must be refused rather than escaping
    # _CHECKOUT_ROOT via rmtree/rename.
    dest = git.clone_to_tmp(str(git_repo), "main")
    assert dest is not None

    # Act / Assert
    with pytest.raises(ValueError, match="unsafe project_id"):
        git.retain_checkout(dest, "../../etc")


@pytest.mark.integration
@pytest.mark.project
def test_remove_retained_rejects_a_path_traversal_project_id():
    # Act / Assert
    with pytest.raises(ValueError, match="unsafe project_id"):
        git.remove_retained("../../etc")
