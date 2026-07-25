import pytest
from pydantic import ValidationError

from entities.request import IndexParams


@pytest.mark.unit
@pytest.mark.project
def test_index_requires_a_source():
    # Act / Assert — neither directory nor repo_url is rejected.
    with pytest.raises(ValidationError, match="exactly one"):
        IndexParams()


@pytest.mark.unit
@pytest.mark.project
def test_index_rejects_two_sources():
    # Act / Assert — both at once is ambiguous and rejected.
    with pytest.raises(ValidationError, match="exactly one"):
        IndexParams(directory="/x", repo_url="git@github.com:o/r.git")


@pytest.mark.unit
@pytest.mark.project
def test_index_accepts_a_single_source():
    # Act / Assert — exactly one source is valid.
    assert IndexParams(directory="/x").directory == "/x"
    assert IndexParams(repo_url="git@github.com:o/r.git").repo_url is not None


@pytest.mark.unit
@pytest.mark.project
def test_blank_unused_source_normalizes_to_none():
    # A client sending "" for the unused source must not misroute dispatch:
    # the blank is treated as absent, leaving exactly one real source.
    params = IndexParams(directory="/x", repo_url="")
    assert params.repo_url is None
    assert params.directory == "/x"
