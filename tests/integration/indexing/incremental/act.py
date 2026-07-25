import os

import pytest

from shared.common.indexing import index_project_graph

PROJECT = "proj_inc"

TWO_FUNCS = "def foo():\n    return 1\n\n\ndef bar():\n    return 2\n"
FOO_EDITED = "def foo():\n    return 999\n\n\ndef bar():\n    return 2\n"
ONLY_FOO = "def foo():\n    return 1\n"


def _project(tmp_path, module_body):
    root = tmp_path / "proj"
    root.mkdir(exist_ok=True)
    (root / "pyproject.toml").write_text("[project]\nname='p'\nversion='0'\n")
    pkg = root / "p"
    pkg.mkdir(exist_ok=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "m.py").write_text(module_body)
    return root


async def _index(root, graph_store, vector_store):
    return await index_project_graph(root, PROJECT, graph_store, vector_store)


@pytest.mark.integration
@pytest.mark.indexing
async def test_reindexing_unchanged_reembeds_nothing(
    tmp_path,
    graph_store,
    vector_store,
):
    # Arrange
    root = _project(tmp_path, TWO_FUNCS)

    # Act
    first = await _index(root, graph_store, vector_store)
    second = await _index(root, graph_store, vector_store)

    # Assert — a no-change re-index embeds nothing and reuses everything.
    assert first.embedded >= 2
    assert second.embedded == 0
    assert second.reused == first.embedded
    assert second.deleted == 0


@pytest.mark.integration
@pytest.mark.indexing
async def test_editing_one_symbol_reembeds_only_it(
    tmp_path,
    graph_store,
    vector_store,
):
    # Arrange
    root = _project(tmp_path, TWO_FUNCS)
    await _index(root, graph_store, vector_store)

    # Act — change foo's body only.
    _project(tmp_path, FOO_EDITED)
    result = await _index(root, graph_store, vector_store)

    # Assert — foo re-embedded, bar reused, nothing deleted.
    assert result.embedded == 1
    assert result.reused >= 1
    assert result.deleted == 0


@pytest.mark.integration
@pytest.mark.indexing
async def test_same_size_mtime_preserved_edit_is_detected(
    tmp_path,
    graph_store,
    vector_store,
):
    # Regression (P03 review): a same-byte-size body edit whose mtime is
    # restored (coarse-mtime FS / tar / cp -p) must still be caught — the
    # per-run source-cache clear is what makes this hold, not the mtime key.
    # Arrange
    root = _project(tmp_path, "def foo():\n    return 1\n")
    module = root / "p" / "m.py"
    original = module.stat()
    await _index(root, graph_store, vector_store)

    # Act — same size, then reset mtime to exactly the pre-edit value.
    module.write_text("def foo():\n    return 2\n")
    os.utime(module, ns=(original.st_atime_ns, original.st_mtime_ns))
    result = await _index(root, graph_store, vector_store)

    # Assert — foo is re-embedded despite the identical (size, mtime) key.
    assert result.embedded == 1


@pytest.mark.integration
@pytest.mark.indexing
async def test_removing_a_symbol_deletes_it_from_graph(
    tmp_path,
    graph_store,
    vector_store,
):
    # Arrange
    root = _project(tmp_path, TWO_FUNCS)
    await _index(root, graph_store, vector_store)

    # Act — drop bar.
    _project(tmp_path, ONLY_FOO)
    result = await _index(root, graph_store, vector_store)

    # Assert — bar is gone from the graph and counted as deleted.
    assert result.deleted >= 1
    rows = await graph_store.execute(
        "MATCH (n:CodeNode {project_id: $p}) WHERE n.name = 'bar' "
        "RETURN n.name AS name",
        {"p": PROJECT},
    )
    assert rows == []
