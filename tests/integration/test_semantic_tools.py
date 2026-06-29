"""Integration tests: content search, semantic tools, and the index cycle.

The semantic search/cluster paths are driven with a monkeypatched embedding
model so the unified pipeline and checkpoint state machine are exercised
without a real model download.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from graphlens_mcp.indexer.workspace import Workspace, default_db_path
from graphlens_mcp.server.tools import (
    tool_find_related,
    tool_list_clusters,
    tool_search_code,
    tool_search_semantic,
)

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = [pytest.mark.integration, pytest.mark.tools]


async def _workspace(root: Path) -> Workspace:
    ws = await Workspace.create(root, default_db_path(root))
    await ws.full_index()
    return ws


class _FakeIndex:
    """A stand-in for semble's index: no model, deterministic, save is a no-op."""

    def __init__(self, hits=None):
        self._hits = hits or []

    def save(self, _path):
        pass

    def search(self, _query, **_kw):
        return self._hits

    def find_related(self, _source, **_kw):
        return self._hits


# ---- search_code (grep replacement) --------------------------------------


async def test_search_code_finds_string_literal(py_project: Path):
    ws = await _workspace(py_project)
    try:
        # A literal that lives inside a function body — invisible to symbol
        # search, the exact case search_code exists for.
        (py_project / "pkg" / "c.py").write_text(
            'MARKER = "needle-in-haystack"\n'
        )
        result = await tool_search_code(ws, "needle-in-haystack")
        assert result.error is None
        assert result.count >= 1
        assert any(
            m.file_path.endswith("c.py") and "needle" in m.text
            for m in result.matches
        )
    finally:
        await ws.close()


async def test_search_code_respects_path_glob(py_project: Path):
    ws = await _workspace(py_project)
    try:
        result = await tool_search_code(ws, "def ", path_glob="*.py")
        assert result.error is None
        assert all(m.file_path.endswith(".py") for m in result.matches)
        none = await tool_search_code(ws, "def ", path_glob="*.rs")
        assert none.count == 0
    finally:
        await ws.close()


async def test_search_code_reports_invalid_pattern(py_project: Path):
    ws = await _workspace(py_project)
    try:
        result = await tool_search_code(ws, "(unclosed[")
        assert result.error is not None
        assert result.matches == []
    finally:
        await ws.close()


# ---- semantic search / find_related --------------------------------------


async def test_search_semantic_unavailable_is_graceful(
    py_project: Path, monkeypatch
):
    semble = pytest.importorskip("semble")

    def boom(*_a, **_k):
        msg = "ProxyError 403 Forbidden fetching model"
        raise RuntimeError(msg)

    monkeypatch.setattr(semble.SembleIndex, "from_path", boom)
    ws = await Workspace.create(py_project, default_db_path(py_project))
    await ws._index_graph()
    try:
        result = await tool_search_semantic(
            ws.store, ws, "add one to a number"
        )
        assert result.available is False
        assert result.reason
        assert result.hits == []
    finally:
        await ws.close()


async def test_search_semantic_bridges_hits_to_nodes(
    py_project: Path, monkeypatch
):
    semble = pytest.importorskip("semble")
    ws = await Workspace.create(py_project, default_db_path(py_project))
    await ws._index_graph()
    try:
        # A hit pointing at helper's source span should bridge to the graph
        # node for helper, so the agent can pivot into get_callers/get_callees.
        helper_file = str((py_project / "pkg" / "a.py").resolve())

        class Hit:
            chunk = type(
                "C",
                (),
                {
                    "content": "def helper(x):\n    return x + 1",
                    "file_path": helper_file,
                    "start_line": 1,
                    "end_line": 2,
                    "language": "python",
                },
            )()
            score = 0.99

        monkeypatch.setattr(
            semble.SembleIndex,
            "from_path",
            lambda *_a, **_k: _FakeIndex([Hit()]),
        )
        result = await tool_search_semantic(ws.store, ws, "increment")
        assert result.available is True
        assert result.count == 1
        hit = result.hits[0]
        assert hit.file_path == helper_file
        assert any(n.name == "helper" for n in hit.nodes)
    finally:
        await ws.close()


async def test_find_related_unknown_node(py_project: Path):
    ws = await _workspace(py_project)
    try:
        result = await tool_find_related(ws.store, ws, "no-such-node")
        assert result.error is not None
    finally:
        await ws.close()


# ---- clusters + unified pipeline / checkpoint ----------------------------


async def test_list_clusters_unavailable_is_graceful(
    py_project: Path, monkeypatch
):
    import model2vec

    def boom(_id):
        msg = "ProxyError 403 Forbidden fetching model"
        raise RuntimeError(msg)

    monkeypatch.setattr(model2vec.StaticModel, "from_pretrained", boom)
    ws = await Workspace.create(py_project, default_db_path(py_project))
    await ws._index_graph()
    try:
        result = await tool_list_clusters(ws.store, ws)
        assert result.available is False
        assert result.reason
    finally:
        await ws.close()


async def test_full_index_checkpoint_stops_at_graph_when_model_blocked(
    py_project: Path, monkeypatch
):
    semble = pytest.importorskip("semble")

    def boom(*_a, **_k):
        msg = "ProxyError 403 Forbidden fetching model"
        raise RuntimeError(msg)

    monkeypatch.setattr(semble.SembleIndex, "from_path", boom)
    ws = await Workspace.create(py_project, default_db_path(py_project))
    try:
        await ws.full_index()
        # Graph completed and was fingerprinted; the semantic tail could not
        # run, so the checkpoint rests at the graph phase (resumable later).
        assert await ws.store.get_meta("index_phase") == "graph"
        assert await ws.store.get_meta("index_root_hash")
        # Resuming is a no-op that does not crash and leaves phase at graph.
        await ws.resume_pending_index()
        assert await ws.store.get_meta("index_phase") == "graph"
    finally:
        await ws.close()


async def test_full_index_completes_pipeline_with_fake_model(
    py_project: Path, monkeypatch
):
    semble = pytest.importorskip("semble")
    np = pytest.importorskip("numpy")
    import model2vec

    monkeypatch.setattr(
        semble.SembleIndex, "from_path", lambda *_a, **_k: _FakeIndex()
    )

    class FakeModel:
        @staticmethod
        def from_pretrained(_id):
            return FakeModel()

        def encode(self, texts):
            return np.ones((len(texts), 3), dtype=np.float32)

    monkeypatch.setattr(model2vec, "StaticModel", FakeModel)

    ws = await Workspace.create(py_project, default_db_path(py_project))
    try:
        await ws.full_index()
        # All three phases ran: graph -> semantic -> clusters -> done.
        assert await ws.store.get_meta("index_phase") == "done"
    finally:
        await ws.close()
