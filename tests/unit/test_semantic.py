"""Unit tests for the semantic layer: pure helpers + model-error degradation.

Pure helpers are deterministic; build paths are exercised with a
monkeypatched embedding model so the model-fetch failure is testable
offline.
"""

from __future__ import annotations

import json

import model2vec
import numpy as np
import pytest

from graphlens_mcp.indexer.semantic import (
    SemanticIndex,
    _embedding_text,
    _is_network_error,
    _model_error_reason,
    _split_identifier,
)

pytestmark = [pytest.mark.unit]


# ---- pure helpers --------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("create_order", ["create", "order"]),
        ("HTTPClient", ["http", "client"]),
        ("create_orderHTTPClient", ["create", "order", "http", "client"]),
        ("pkg.sub.MyClass", ["pkg", "sub", "my", "class"]),
        ("value2text", ["value", "2", "text"]),
    ],
)
def test_split_identifier(name, expected):
    assert _split_identifier(name) == expected


def test_embedding_text_folds_in_signature_and_docstring():
    meta = json.dumps(
        {"signature": "def f(x: int) -> int", "docstring": "Adds one.\nmore"}
    )
    text = _embedding_text(
        {"qualified_name": "m.f", "name": "f", "metadata_json": meta}
    )
    assert "m.f" in text
    assert "def f(x: int) -> int" in text
    assert "Adds one." in text
    # The docstring is now folded in more fully (not just the summary line) so
    # search-by-meaning has more signal to match against.
    assert "more" in text


def test_is_network_error_and_reason():
    netty = RuntimeError("ProxyError: 403 Forbidden from huggingface")
    assert _is_network_error(netty)
    assert "model" in _model_error_reason(netty).lower()

    other = ValueError("some bug")
    assert not _is_network_error(other)


# ---- graceful degradation (model-fetch failure) --------------------------


async def test_search_degrades_gracefully_on_build_failure(
    tmp_path, monkeypatch
):
    """When the model fetch fails, build() propagates the reason to search."""

    def boom(*_a, **_k):
        msg = "ProxyError 403 Forbidden while fetching model"
        raise RuntimeError(msg)

    monkeypatch.setattr(model2vec.StaticModel, "from_pretrained", boom)

    from graphlens_mcp.store.sqlite_store import SqliteStore

    store = await SqliteStore.create(tmp_path / "test.db")
    try:
        await store._conn.execute(
            "INSERT INTO nodes(id, kind, qualified_name, name, file_path) "
            "VALUES('n1', 'function', 'pkg.foo', 'foo', '/x/a.py')"
        )
        await store._conn.execute(
            "INSERT INTO files(path, hash, mtime, size, status, language) "
            "VALUES('/x/a.py', 'abc', 1.0, 10, 'ok', 'python')"
        )
        await store._conn.commit()

        idx = SemanticIndex()
        avail = await idx.build(store)
        assert avail.ok is False
        assert "model" in avail.reason.lower()

        # Sticky reason: subsequent search does not retry the model.
        resp = await idx.search(store, "anything", top_k=3)
        assert resp.available is False
        assert resp.reason and "model" in resp.reason.lower()
        assert idx.availability.ok is False
    finally:
        await store.close()


async def test_search_returns_node_hits_with_fake_model(tmp_path, monkeypatch):
    """search() returns SemanticHit objects with graph node metadata."""

    class FakeModel:
        @staticmethod
        def from_pretrained(_id):
            return FakeModel()

        def encode(self, texts):
            return np.ones((len(texts), 3), dtype=np.float32)

    monkeypatch.setattr(model2vec, "StaticModel", FakeModel)

    from graphlens_mcp.store.sqlite_store import SqliteStore

    store = await SqliteStore.create(tmp_path / "test.db")
    try:
        await store._conn.execute(
            "INSERT INTO files(path, hash, mtime, size, status, language) "
            "VALUES('/x/a.py', 'abc', 1.0, 10, 'ok', 'python')"
        )
        await store._conn.execute(
            "INSERT INTO nodes(id, kind, qualified_name, name, file_path) "
            "VALUES('n1', 'function', 'pkg.helper', 'helper', '/x/a.py')"
        )
        await store._conn.commit()

        idx = SemanticIndex()
        avail = await idx.build(store)
        assert avail.ok is True

        resp = await idx.search(store, "helper function", top_k=5)
        assert resp.available is True
        assert len(resp.hits) == 1
        hit = resp.hits[0]
        assert hit.node_id == "n1"
        assert hit.kind == "function"
        assert hit.name == "helper"
    finally:
        await store.close()
