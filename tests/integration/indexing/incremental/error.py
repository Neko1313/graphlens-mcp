import pytest

from shared.common.indexing import index_project_graph

PROJECT = "proj_fail"


class _FlakyVectors:
    """Wraps a real vector store and fails the first upsert once."""

    def __init__(self, real):
        self._real = real
        self._fail_next_upsert = True

    async def ensure_collection(self, collection, dim):
        return await self._real.ensure_collection(collection, dim)

    async def upsert(self, collection, rows):
        if self._fail_next_upsert:
            self._fail_next_upsert = False
            msg = "vector store down"
            raise RuntimeError(msg)
        return await self._real.upsert(collection, rows)

    async def search(self, *args, **kwargs):
        return await self._real.search(*args, **kwargs)

    async def delete(self, collection, filter_expr):
        return await self._real.delete(collection, filter_expr)

    async def drop_collection(self, collection):
        return await self._real.drop_collection(collection)

    async def check(self):
        return await self._real.check()

    async def close(self):
        return await self._real.close()


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
async def test_a_failed_vector_write_self_heals_on_retry(
    tmp_path,
    graph_store,
    vector_store,
):
    # Regression (P03 review): vectors are written before the graph
    # content_hash, so a failed embed-store leaves the diff baseline stale and
    # the retry re-embeds — never a graph node with a permanently missing
    # vector.
    # Arrange
    root = _project(tmp_path)
    flaky = _FlakyVectors(vector_store)

    # Act — first index fails inside the vector upsert.
    with pytest.raises(RuntimeError, match="vector store down"):
        await index_project_graph(root, PROJECT, graph_store, flaky)
    # Retry with the same (now-healthy) store, file unchanged.
    result = await index_project_graph(root, PROJECT, graph_store, flaky)

    # Assert — the node is (re-)embedded, not skipped as already-current.
    assert result.embedded >= 1
