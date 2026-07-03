"""Unit tests for the store's semantic bridge, fingerprint, meta."""

from __future__ import annotations

import pytest
from graphlens import Node, NodeKind, make_node_id
from graphlens.utils import Span

from tests.conftest import graph_of

pytestmark = [pytest.mark.unit, pytest.mark.store]

FILE = "/proj/m.py"
META = ("hash", 1.0, 10, "ok", "python")


def node_with_span(
    qualified_name: str,
    start: int,
    end: int,
    *,
    kind: NodeKind = NodeKind.FUNCTION,
    file_path: str = FILE,
) -> Node:
    return Node(
        id=make_node_id("test", qualified_name, kind.value),
        kind=kind,
        qualified_name=qualified_name,
        name=qualified_name.rsplit(".", 1)[-1],
        file_path=file_path,
        span=Span(start_line=start, start_col=0, end_line=end, end_col=0),
    )


async def _apply(store, nodes, file_path=FILE):
    await store.apply_patch(graph_of(nodes, []), file_path, *META)


# ---- span-overlap bridge -------------------------------------------------


async def test_nodes_overlapping_prefers_tightest_enclosing_symbol(store):
    outer = node_with_span("m.Outer", 1, 100, kind=NodeKind.CLASS)
    method = node_with_span("m.Outer.method", 10, 20, kind=NodeKind.METHOD)
    far = node_with_span("m.far", 200, 210)
    await _apply(store, [outer, method, far])

    hits = await store.nodes_overlapping(FILE, 12, 15, limit=5)
    names = [h["qualified_name"] for h in hits]

    # The method tightly wraps lines 12-15, so it must rank ahead of the
    # whole class that merely contains the range; the far node never matches.
    assert names[0] == "m.Outer.method"
    assert "m.Outer" in names
    assert "m.far" not in names


async def test_nodes_overlapping_is_scoped_to_the_file(store):
    here = node_with_span("m.here", 1, 5)
    await _apply(store, [here])
    assert await store.nodes_overlapping("/other.py", 1, 5) == []


# ---- fingerprint + meta --------------------------------------------------


async def test_files_fingerprint_is_stable_and_content_sensitive(store):
    empty = await store.files_fingerprint()
    await _apply(store, [node_with_span("m.f", 1, 5)])
    after = await store.files_fingerprint()
    assert after != empty
    assert after == await store.files_fingerprint()  # stable

    # A different file hash changes the fingerprint.
    await store.apply_patch(
        graph_of([node_with_span("m.f", 1, 5)], []),
        FILE,
        "different-hash",
        1.0,
        10,
        "ok",
        "python",
    )
    assert await store.files_fingerprint() != after


async def test_public_meta_get_set(store):
    assert await store.get_meta("k") is None
    await store.set_meta("k", "v")
    assert await store.get_meta("k") == "v"
    await store.set_meta("k", "v2")
    assert await store.get_meta("k") == "v2"


# ---- embedding storage ---------------------------------------------------


async def test_store_and_retrieve_embeddings(store):
    """store_embeddings persists raw bytes; get_embedding_rows joins nodes."""
    np = pytest.importorskip("numpy")

    a = node_with_span("m.fn_a", 1, 5)
    b = node_with_span("m.fn_b", 6, 10)
    await _apply(store, [a, b])

    vec_a = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    vec_b = np.array([0.0, 1.0, 0.0], dtype=np.float32)
    await store.store_embeddings(
        [(a.id, vec_a.tobytes()), (b.id, vec_b.tobytes())]
    )

    rows = await store.get_embedding_rows()
    assert len(rows) == 2
    # Rows ordered by node_id; verify round-trip fidelity.
    by_id = {r["node_id"]: r for r in rows}
    assert a.id in by_id and b.id in by_id
    recovered = np.frombuffer(bytes(by_id[a.id]["vector"]), dtype=np.float32)
    assert np.allclose(recovered, vec_a)
    # Node metadata is joined in.
    assert by_id[a.id]["qualified_name"] == "m.fn_a"


async def test_store_embeddings_replaces_previous(store):
    """Calling store_embeddings again wipes and replaces the old set."""
    np = pytest.importorskip("numpy")

    a = node_with_span("m.fn_a", 1, 5)
    await _apply(store, [a])

    vec = np.array([1.0, 0.0], dtype=np.float32)
    await store.store_embeddings([(a.id, vec.tobytes())])
    await store.store_embeddings([])  # replace with empty
    assert await store.get_embedding_rows() == []


async def test_get_embedding_rows_filters_dangling(store):
    """Embeddings for deleted nodes are excluded (JOIN with nodes)."""
    np = pytest.importorskip("numpy")

    a = node_with_span("m.fn_a", 1, 5)
    await _apply(store, [a])
    vec = np.array([1.0, 0.0], dtype=np.float32)
    await store.store_embeddings([(a.id, vec.tobytes())])

    await store.delete_file(FILE)
    assert await store.get_embedding_rows() == []
