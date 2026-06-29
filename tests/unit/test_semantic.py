"""Unit tests for the semantic layer: pure helpers + graceful degradation.

These avoid any real model download: pure helpers are deterministic, and the
build/cluster paths are exercised with a monkeypatched embedding model so the
graceful-degradation and end-to-end clustering logic are testable offline.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from graphlens_mcp.indexer import semantic as sem
from graphlens_mcp.indexer.semantic import (
    SemanticIndex,
    _embedding_text,
    _is_network_error,
    _label_for,
    _model_error_reason,
    _split_identifier,
    semantic_availability,
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
    # "order" is most frequent and distinctive; "get"/"create" are generic
    # stopwords and "validate"/"repo" trail. Label leads with "order".
    assert terms[0] == "order"
    assert "get" not in terms  # stopword
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


def test_semantic_availability_present_in_this_env():
    # The test env installs the [semantic] extra, so availability is ok here;
    # the absent-extra path is covered by the ImportError guard in the source.
    assert semantic_availability().ok


# ---- cluster assembly (deterministic, numpy only) ------------------------


def test_assemble_clusters_excludes_noise_and_scores_by_centroid():
    np = pytest.importorskip("numpy")
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
    # Two members are HDBSCAN noise (label -1) and must be dropped.
    labels = np.array([7, 7, 7, -1, -1])

    comp = sem._assemble_clusters(nodes, vectors, labels)

    assert len(comp.clusters) == 1
    cluster = comp.clusters[0]
    assert cluster["id"] == 1  # renumbered to dense, size-sorted ids
    assert cluster["size"] == 3
    assert "auth" in cluster["label"]
    assigned = {a["node_id"] for a in comp.assignments}
    assert assigned == {"a0", "a1", "a2"}
    # Members identical to their centroid score ~1.0.
    assert all(abs(a["score"] - 1.0) < 1e-6 for a in comp.assignments)


# ---- graceful degradation + e2e with a fake model ------------------------


async def test_search_degrades_gracefully_on_build_failure(
    tmp_path, monkeypatch
):
    semble = pytest.importorskip("semble")

    def boom(*_a, **_k):
        msg = "ProxyError 403 Forbidden while fetching model"
        raise RuntimeError(msg)

    monkeypatch.setattr(semble.SembleIndex, "from_path", boom)
    idx = SemanticIndex(tmp_path, tmp_path / ".graphlens" / "idx")

    resp = await idx.search("anything", top_k=3)

    assert resp.available is False
    assert resp.reason and "model" in resp.reason.lower()
    # The reason is sticky for the session so we don't retry a blocked fetch.
    assert idx.availability.ok is False


async def test_compute_clusters_returns_none_below_min(monkeypatch):
    idx = SemanticIndex(Path("/x"), Path("/x/idx"))
    assert await idx.compute_clusters([{"id": "only"}]) is None


async def test_compute_clusters_end_to_end_with_fake_model(monkeypatch):
    np = pytest.importorskip("numpy")
    pytest.importorskip("sklearn")
    import model2vec

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
    idx = SemanticIndex(Path("/x"), Path("/x/idx"))

    comp = await idx.compute_clusters(nodes)

    # The whole pipeline (embed -> normalize -> cluster -> assemble) ran
    # offline; we assert structural validity rather than an exact partition
    # (HDBSCAN's exact labels on synthetic data are not contractual).
    assert comp is not None
    node_ids = {n["id"] for n in nodes}
    assert {a["node_id"] for a in comp.assignments}.issubset(node_ids)
    assert all(c["size"] >= 1 for c in comp.clusters)
    assert all(-1.0001 <= a["score"] <= 1.0001 for a in comp.assignments)
