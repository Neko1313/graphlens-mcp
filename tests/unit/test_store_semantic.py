"""Unit tests for the store's semantic bridge, clusters, fingerprint, meta."""

from __future__ import annotations

import json

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


# ---- cluster storage -----------------------------------------------------


async def test_cluster_round_trip(store):
    a = node_with_span("m.login", 1, 5)
    b = node_with_span("m.authToken", 6, 10)
    c = node_with_span("m.charge", 11, 15)
    await _apply(store, [a, b, c])

    clusters = [
        {"id": 1, "label": "auth", "size": 2, "terms": ["auth", "login"]},
        {"id": 2, "label": "pay", "size": 1, "terms": ["pay"]},
    ]
    assignments = [
        {"node_id": a.id, "cluster_id": 1, "score": 0.9},
        {"node_id": b.id, "cluster_id": 1, "score": 0.8},
        {"node_id": c.id, "cluster_id": 2, "score": 0.7},
    ]
    await store.replace_clusters(clusters, assignments)

    assert await store.cluster_count() == 2

    # min_size filters out the singleton cluster.
    listed = await store.list_clusters(min_size=2)
    assert [row["id"] for row in listed] == [1]

    got = await store.get_cluster(1)
    assert got["label"] == "auth"
    assert json.loads(got["terms"]) == ["auth", "login"]

    # Members come back ordered by score (descending).
    members = await store.get_cluster_members(1)
    assert [m["id"] for m in members] == [a.id, b.id]

    assert await store.get_cluster_id_for_node(a.id) == 1
    assert await store.get_cluster_id_for_node("missing") is None


async def test_replace_clusters_overwrites_previous(store):
    a = node_with_span("m.one", 1, 5)
    await _apply(store, [a])
    await store.replace_clusters(
        [{"id": 1, "label": "x", "size": 1, "terms": []}],
        [{"node_id": a.id, "cluster_id": 1, "score": 1.0}],
    )
    await store.replace_clusters([], [])
    assert await store.cluster_count() == 0
    assert await store.get_cluster_id_for_node(a.id) is None


async def test_cluster_members_filter_dangling_assignments(store):
    a = node_with_span("m.gone", 1, 5)
    await _apply(store, [a])
    await store.replace_clusters(
        [{"id": 1, "label": "x", "size": 1, "terms": []}],
        [{"node_id": a.id, "cluster_id": 1, "score": 1.0}],
    )
    # The node's file is deleted; its cluster assignment is now dangling and
    # must be filtered out at read time (matching the edge-query model).
    await store.delete_file(FILE)
    assert await store.get_cluster_members(1) == []


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
