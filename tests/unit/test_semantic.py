"""Unit tests for the semantic layer: pure helpers + model-error degradation.

Pure helpers are deterministic; build/cluster paths are exercised with a
monkeypatched embedding model so the model-fetch failure and end-to-end
clustering logic are testable offline.
"""

from __future__ import annotations

import json

import model2vec
import numpy as np
import pytest

from graphlens_mcp.indexer.semantic import (
    SemanticIndex,
    _assemble_clusters,
    _embedding_text,
    _is_network_error,
    _label_for,
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


def test_label_for_ranks_by_frequency_and_drops_stopwords():
    label, terms = _label_for(
        ["create_order", "validate_order", "OrderRepo", "get_order"]
    )
    assert terms[0] == "order"
    assert "get" not in terms
    assert label.startswith("order")


def test_label_for_falls_back_when_nothing_distinctive():
    label, terms = _label_for(["get", "set", "run"])
    assert label == "misc"
    assert terms == []


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
    assert "more" not in text  # only the summary line is kept


def test_is_network_error_and_reason():
    netty = RuntimeError("ProxyError: 403 Forbidden from huggingface")
    assert _is_network_error(netty)
    assert "model" in _model_error_reason(netty).lower()

    other = ValueError("some bug")
    assert not _is_network_error(other)


# ---- cluster assembly (deterministic, numpy only) ------------------------


def test_assemble_clusters_excludes_noise_and_scores_by_centroid():
    nodes = [
        {"id": "a0", "qualified_name": "m.authLogin0"},
        {"id": "a1", "qualified_name": "m.authLogin1"},
        {"id": "a2", "qualified_name": "m.authToken2"},
        {"id": "p0", "qualified_name": "m.payCharge0"},
        {"id": "p1", "qualified_name": "m.payCharge1"},
    ]
    vectors = np.array(
        [
            [1.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 1.0, 0.0],
        ],
        dtype=np.float32,
    )
    labels = np.array([7, 7, 7, -1, -1])

    comp = _assemble_clusters(nodes, vectors, labels)

    assert len(comp.clusters) == 1
    cluster = comp.clusters[0]
    assert cluster["id"] == 1
    assert cluster["size"] == 3
    assert "auth" in cluster["label"]
    assigned = {a["node_id"] for a in comp.assignments}
    assert assigned == {"a0", "a1", "a2"}
    assert all(abs(a["score"] - 1.0) < 1e-6 for a in comp.assignments)


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


async def test_compute_clusters_returns_none_when_no_embeddings():
    """compute_clusters returns None when the vector cache is empty."""

    class _MockStore:
        async def get_embedding_rows(self):
            return []

    idx = SemanticIndex()
    result = await idx.compute_clusters(_MockStore())
    assert result is None


# ---- e2e with a fake model -----------------------------------------------


async def test_compute_clusters_end_to_end_with_fake_model(monkeypatch):
    class FakeModel:
        @staticmethod
        def from_pretrained(_id):
            return FakeModel()

        def encode(self, texts):
            rows = []
            for text in texts:
                if "auth" in text:
                    rows.append([1.0, 0.0, 0.0])
                elif "pay" in text:
                    rows.append([0.0, 1.0, 0.0])
                else:
                    rows.append([0.0, 0.0, 1.0])
            return np.array(rows, dtype=np.float32)

    monkeypatch.setattr(model2vec, "StaticModel", FakeModel)

    nodes = [
        {
            "id": f"a{i}",
            "qualified_name": f"m.authLogin{i}",
            "metadata_json": None,
        }
        for i in range(4)
    ] + [
        {
            "id": f"p{i}",
            "qualified_name": f"m.payCharge{i}",
            "metadata_json": None,
        }
        for i in range(4)
    ]

    idx = SemanticIndex()
    fake_vecs = np.array(
        [[1.0, 0.0, 0.0]] * 4 + [[0.0, 1.0, 0.0]] * 4, dtype=np.float32
    )
    idx._vectors = fake_vecs
    idx._node_ids = [n["id"] for n in nodes]
    idx._node_meta = [
        {
            "kind": "function",
            "name": n["qualified_name"].split(".")[-1],
            "qualified_name": n["qualified_name"],
            "file_path": None,
        }
        for n in nodes
    ]
    idx._dirty = False

    class _MockStore:
        async def get_embedding_rows(self):
            return []

    comp = await idx.compute_clusters(_MockStore())

    assert comp is not None
    node_ids = {n["id"] for n in nodes}
    assert {a["node_id"] for a in comp.assignments}.issubset(node_ids)
    assert all(c["size"] >= 1 for c in comp.clusters)
    assert all(-1.0001 <= a["score"] <= 1.0001 for a in comp.assignments)


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
