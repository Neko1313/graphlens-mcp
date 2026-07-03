"""
Integration tests for the unified index pipeline (graph -> semantic).

Driven with a monkeypatched embedding model so the pipeline and checkpoint
state machine are exercised without a real model download.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import model2vec
import numpy as np
import pytest

from graphlens_mcp.indexer.workspace import Workspace, default_db_path

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = [pytest.mark.integration, pytest.mark.tools]


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
        # Both phases ran: graph -> semantic embeddings -> done.
        assert await ws.store.get_meta("index_phase") == "done"
    finally:
        await ws.close()
