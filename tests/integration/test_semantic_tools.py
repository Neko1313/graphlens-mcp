"""Integration tests: content search, semantic tools, and the index cycle.

The semantic search/cluster paths are driven with a monkeypatched embedding
model so the unified pipeline and checkpoint state machine are exercised
without a real model download.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import model2vec
import numpy as np
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


# ---- search_code (grep replacement) --------------------------------------


async def test_search_code_finds_string_literal(py_project: Path):
    ws = await _workspace(py_project)
    try:
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


# ---- semantic search (node-based, no chunk bridge) -----------------------


async def test_search_semantic_unavailable_is_graceful(
    py_project: Path, monkeypatch
):
    def boom(*_a, **_k):
        msg = "ProxyError 403 Forbidden fetching model"
        raise RuntimeError(msg)

    monkeypatch.setattr(model2vec.StaticModel, "from_pretrained", boom)
    ws = await Workspace.create(py_project, default_db_path(py_project))
    await ws._index_graph()
    # Build the graph nodes first so build() has something to embed, then
    # try to build the semantic index (which will fail at model load).
    try:
        avail = await ws.semantic.build(ws.store)
        assert avail.ok is False
        # Subsequent search uses the sticky reason without re-trying the model.
        result = await tool_search_semantic(
            ws.store, ws, "add one to a number"
        )
        assert result.available is False
        assert result.reason
        assert result.hits == []
    finally:
        await ws.close()


async def test_search_semantic_returns_nodes(py_project: Path, monkeypatch):
    """Hits are graph nodes directly — no chunk→node bridge needed."""

    class FakeModel:
        @staticmethod
        def from_pretrained(_id):
            return FakeModel()

        def encode(self, texts):
            # All identical unit vectors — every node scores equally.
            return np.ones((len(texts), 3), dtype=np.float32)

    monkeypatch.setattr(model2vec, "StaticModel", FakeModel)
    ws = await Workspace.create(py_project, default_db_path(py_project))
    await ws._index_graph()
    try:
        await ws.semantic.build(ws.store)
        result = await tool_search_semantic(ws.store, ws, "increment")
        assert result.available is True
        assert result.count >= 1
        hit = result.hits[0]
        # Each hit is a node — node_id, kind, name are all present.
        assert hit.node_id
        assert hit.kind in ("function", "method", "class")
        assert hit.name
    finally:
        await ws.close()


async def test_find_related_unknown_node(py_project: Path):
    ws = await _workspace(py_project)
    try:
        result = await tool_find_related(ws.store, ws, "no-such-node")
        assert result.error is not None
    finally:
        await ws.close()


async def test_find_related_returns_similar_nodes(
    py_project: Path, monkeypatch
):
    class FakeModel:
        @staticmethod
        def from_pretrained(_id):
            return FakeModel()

        def encode(self, texts):
            return np.ones((len(texts), 3), dtype=np.float32)

    monkeypatch.setattr(model2vec, "StaticModel", FakeModel)
    ws = await Workspace.create(py_project, default_db_path(py_project))
    await ws._index_graph()
    try:
        await ws.semantic.build(ws.store)
        # Pick any indexed node as source.
        rows = await ws.store.get_nodes_for_clustering()
        if not rows:
            pytest.skip("No indexable nodes in fixture project")
        source_id = rows[0]["id"]
        result = await tool_find_related(ws.store, ws, source_id)
        assert result.available is True
        # Hits should not include the source node itself.
        assert all(h.node_id != source_id for h in result.hits)
    finally:
        await ws.close()


# ---- clusters + unified pipeline / checkpoint ----------------------------


async def test_list_clusters_unavailable_is_graceful(
    py_project: Path, monkeypatch
):
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
    def boom(*_a, **_k):
        msg = "ProxyError 403 Forbidden fetching model"
        raise RuntimeError(msg)

    monkeypatch.setattr(model2vec.StaticModel, "from_pretrained", boom)
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
