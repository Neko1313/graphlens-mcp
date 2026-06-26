"""Unit tests for graph path normalization (the mixed abs/rel path fix)."""

from __future__ import annotations

from pathlib import Path

import pytest
from graphlens import NodeKind

from graphlens_mcp.indexer.workspace import _normalize_graph_paths
from tests.conftest import graph_of, make_node

pytestmark = [pytest.mark.unit, pytest.mark.paths]


def test_relative_and_absolute_paths_collapse_to_one_absolute(tmp_path: Path):
    # Arrange: a FILE node (relative path, as adapters emit) and a FUNCTION node
    # (absolute path) for the SAME file, plus a fileless MODULE node.
    abs_a = str((tmp_path / "pkg" / "a.py").resolve())
    file_node = make_node("pkg.a", kind=NodeKind.FILE, file_path="pkg/a.py")
    func_node = make_node("pkg.a.helper", kind=NodeKind.FUNCTION, file_path=abs_a)
    module_node = make_node("pkg", kind=NodeKind.MODULE, file_path=None)
    graph = graph_of([file_node, func_node, module_node], [])

    # Act
    normalized = _normalize_graph_paths(graph, tmp_path)

    # Assert: both nodes now key on the same absolute path; the fileless node stays None
    paths = {n.id: n.file_path for n in normalized.nodes.values()}
    assert paths[file_node.id] == abs_a
    assert paths[func_node.id] == abs_a
    assert paths[module_node.id] is None


def test_normalization_preserves_ids_and_node_count(tmp_path: Path):
    # Arrange
    nodes = [
        make_node("pkg.a", kind=NodeKind.FILE, file_path="pkg/a.py"),
        make_node("pkg.a.helper", file_path="pkg/a.py"),
    ]
    graph = graph_of(nodes, [])
    # Act
    normalized = _normalize_graph_paths(graph, tmp_path)
    # Assert: identity (ids) is stable — this is what lets edges reconnect
    assert set(normalized.nodes) == {n.id for n in nodes}
