"""Shared fixtures and small builders for the graphlens-mcp test suite."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from graphlens import (
    GraphLens,
    Node,
    NodeKind,
    Relation,
    RelationKind,
    make_node_id,
)

from graphlens_mcp.store.sqlite_store import SqliteStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

PROJECT = "test"


def make_node(
    qualified_name: str,
    *,
    kind: NodeKind = NodeKind.FUNCTION,
    file_path: str | None = None,
) -> Node:
    """Build a Node with a stable id derived the same way the adapters do."""
    node_id = make_node_id(PROJECT, qualified_name, kind.value)
    return Node(
        id=node_id,
        kind=kind,
        qualified_name=qualified_name,
        name=qualified_name.rsplit(".", 1)[-1],
        file_path=file_path,
    )


def make_relation(source: Node, target: Node, kind: RelationKind) -> Relation:
    """Build a Relation between two nodes."""
    return Relation(source_id=source.id, target_id=target.id, kind=kind)


def graph_of(nodes: list[Node], relations: list[Relation]) -> GraphLens:
    """Assemble a GraphLens from nodes and relations."""
    g = GraphLens()
    for n in nodes:
        g.add_node(n)
    for r in relations:
        g.add_relation(r)
    return g


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    """A fresh SqliteStore backed by a temp database, closed on teardown."""
    s = await SqliteStore.create(tmp_path / "graph.db")
    try:
        yield s
    finally:
        await s.close()


@pytest.fixture
def py_project(tmp_path: Path) -> Path:
    """A tiny two-module Python package with a cross-file call (b.use -> a.helper)."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "a.py").write_text(
        "def helper(x):\n    return x + 1\n\n\ndef main():\n    return helper(41)\n"
    )
    (pkg / "b.py").write_text(
        "from pkg.a import helper\n\n\ndef use():\n    return helper(1)\n"
    )
    return tmp_path


@pytest.fixture
def py_workspace(tmp_path: Path) -> Path:
    """
    A uv-style workspace with two member packages and a cross-file call.

    The repo root is a *virtual* workspace root: it carries
    ``[tool.uv.workspace]`` but no ``[project]`` section, so the engine
    indexes the members (``backend``, ``shared``) as the real package roots.
    ``backend.pkg.b.use`` calls ``backend.pkg.a.helper`` across files inside
    one member — the relationship a member edit must not break.
    """
    root = tmp_path
    (root / "pyproject.toml").write_text(
        '[tool.uv.workspace]\nmembers = ["packages/*"]\n'
    )
    backend = root / "packages" / "backend"
    pkg = backend / "pkg"
    pkg.mkdir(parents=True)
    (backend / "pyproject.toml").write_text(
        '[project]\nname = "backend"\nversion = "0.1.0"\n'
    )
    (pkg / "__init__.py").write_text("")
    (pkg / "a.py").write_text(
        "def helper(x):\n    return x + 1\n\n\ndef main():\n    return helper(41)\n"
    )
    (pkg / "b.py").write_text(
        "from pkg.a import helper\n\n\ndef use():\n    return helper(1)\n"
    )

    shared = root / "packages" / "shared"
    spkg = shared / "shared"
    spkg.mkdir(parents=True)
    (shared / "pyproject.toml").write_text(
        '[project]\nname = "shared"\nversion = "0.1.0"\n'
    )
    (spkg / "__init__.py").write_text("")
    (spkg / "util.py").write_text("def shared_helper():\n    return 7\n")
    return root
