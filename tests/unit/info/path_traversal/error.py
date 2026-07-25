import pytest

from shared.common import source


@pytest.mark.unit
@pytest.mark.info
async def test_read_file_refuses_an_absolute_path_escape(tmp_path):
    # root / "/etc/passwd" == Path("/etc/passwd") — pathlib treats an
    # absolute right-hand operand as a full override of the left side.
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret")
    root = tmp_path / "project"
    root.mkdir()

    # Act / Assert
    assert await source.read_file(root, str(outside)) is None
    assert await source.is_file(root, str(outside)) is False


@pytest.mark.unit
@pytest.mark.info
async def test_read_file_refuses_dot_dot_traversal(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    root = tmp_path / "project"
    root.mkdir()

    # Act / Assert
    assert await source.read_file(root, "../outside.txt") is None
    assert await source.is_file(root, "../outside.txt") is False


@pytest.mark.unit
@pytest.mark.info
async def test_read_file_still_reads_a_root_relative_path(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "m.py").write_text("def f(): pass")

    # Act / Assert — the fix must not break the normal case.
    assert await source.read_file(root, "m.py") == "def f(): pass"
    assert await source.is_file(root, "m.py") is True
