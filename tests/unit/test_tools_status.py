"""Unit tests for the tool layer: status aggregation + node-info metadata surfacing."""

from __future__ import annotations

import json

import pytest
from graphlens import GraphLens, Node, NodeKind, make_node_id

from graphlens_mcp.indexer.workspace import Workspace
from graphlens_mcp.server.tools import _aggregate_status, _first_meta, tool_get_node_info
from tests.conftest import graph_of, make_node

pytestmark = [pytest.mark.unit, pytest.mark.tools]


async def test_aggregate_status_takes_worst_across_returned_files(store):
    # Arrange: one file indexed 'ok', another 'skeleton'
    await store.apply_patch(
        graph_of([make_node("a.x", file_path="/a.py")], []), "/a.py", "h", 1.0, 1, "ok", "python"
    )
    await store.apply_patch(
        graph_of([make_node("b.y", file_path="/b.py")], []),
        "/b.py",
        "h",
        1.0,
        1,
        "skeleton",
        "python",
    )
    rows = [{"file_path": "/a.py"}, {"file_path": "/b.py"}]
    # Act / Assert: even with an 'ok' base, a skeleton file in the result degrades it
    assert await _aggregate_status(store, "ok", rows) == "skeleton"
    assert await _aggregate_status(store, "ok", [{"file_path": "/a.py"}]) == "ok"


def test_first_meta_picks_first_present_string_key():
    meta = json.dumps({"docstring": "Adds one.", "signature": "(x: int) -> int"})
    assert _first_meta(meta, ("signature", "sig")) == "(x: int) -> int"
    assert _first_meta(meta, ("doc", "docstring")) == "Adds one."
    assert _first_meta(meta, ("missing",)) is None
    assert _first_meta(None, ("signature",)) is None


async def test_get_node_info_surfaces_signature_and_docstring(store, tmp_path):
    # Arrange: a fileless node carrying signature/docstring metadata (no disk read needed)
    node = Node(
        id=make_node_id("test", "pkg.add", NodeKind.FUNCTION.value),
        kind=NodeKind.FUNCTION,
        qualified_name="pkg.add",
        name="add",
        file_path=None,
        metadata={"signature": "(x: int) -> int", "docstring": "Adds one."},
    )
    g = GraphLens()
    g.add_node(node)
    await store.apply_structural(g)
    ws = Workspace(store, tmp_path)
    # Act
    info = await tool_get_node_info(store, ws, node.id)
    # Assert
    assert info.signature == "(x: int) -> int"
    assert info.docstring == "Adds one."
