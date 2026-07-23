from pathlib import Path
from types import SimpleNamespace

import pytest

from shared.common.indexing.temporal import content_hash

ROOT = Path("/repo")


def _node(qualified_name="mod.f", kind="function", file_path="m.py",
          span=None, metadata=None):
    return SimpleNamespace(
        qualified_name=qualified_name,
        name=qualified_name.rsplit(".", 1)[-1],
        kind=SimpleNamespace(value=kind),
        file_path=file_path,
        span=span,
        metadata=metadata or {},
    )


@pytest.mark.unit
@pytest.mark.temporal
def test_same_content_hashes_the_same():
    # Arrange
    left = _node(metadata={"sig": "() -> int"})
    right = _node(metadata={"sig": "() -> int"})

    # Act / Assert — idempotency rests on this equality.
    assert content_hash(ROOT, left) == content_hash(ROOT, right)


@pytest.mark.unit
@pytest.mark.temporal
def test_metadata_change_changes_the_hash():
    # Arrange
    before = _node(metadata={"sig": "() -> int"})
    after = _node(metadata={"sig": "() -> str"})

    # Act / Assert — a changed signature must read as an update.
    assert content_hash(ROOT, before) != content_hash(ROOT, after)


@pytest.mark.unit
@pytest.mark.temporal
def test_span_move_changes_the_hash():
    # Arrange
    span_a = SimpleNamespace(start_line=1, start_col=0, end_line=3, end_col=0)
    span_b = SimpleNamespace(start_line=9, start_col=0, end_line=11, end_col=0)

    # Act / Assert
    assert content_hash(ROOT, _node(span=span_a)) != content_hash(
        ROOT, _node(span=span_b),
    )
