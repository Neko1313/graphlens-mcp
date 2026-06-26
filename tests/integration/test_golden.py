"""Golden test: per-file incremental indexing reproduces the batch full index.

This is the safety net the architecture calls for. We scope it to what incremental
indexing actually contracts to reproduce:

* For a single self-contained file, incremental indexing must reproduce the batch
  graph EXACTLY (file-bearing nodes + all file-owned edges) — this guards the path
  normalization, ownership filter and patch machinery.
* Across files, the file-bearing NODE set must still match. Cross-file *call target*
  resolution legitimately differs (single-file analysis cannot see another file's
  definitions — the documented transitive-freshness limitation), so edge identity is
  only asserted in the single-file case.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from graphlens import GraphLens, Node, NodeKind
from graphlens.diffing import diff_graphs

from graphlens_mcp.indexer.workspace import Workspace, default_db_path

pytestmark = [pytest.mark.integration, pytest.mark.golden]


async def _file_node_graph(store) -> GraphLens:
    """Reconstruct a graph of the file-bearing nodes held by the store."""
    g = GraphLens()
    async with store._conn.execute(
        "SELECT id, kind, qualified_name, name, file_path FROM nodes WHERE file_path IS NOT NULL"
    ) as cur:
        for r in await cur.fetchall():
            g.add_node(
                Node(
                    id=r["id"],
                    kind=NodeKind(r["kind"]),
                    qualified_name=r["qualified_name"],
                    name=r["name"],
                    file_path=r["file_path"],
                )
            )
    return g


async def _file_owned_edges(store) -> set[tuple[str, str, str]]:
    async with store._conn.execute(
        "SELECT e.source_id, e.target_id, e.kind FROM edges e "
        "JOIN nodes n ON n.id = e.source_id "
        "WHERE n.file_path IS NOT NULL AND e.kind != 'communicates_with'"
    ) as cur:
        return {(r["source_id"], r["target_id"], r["kind"]) for r in await cur.fetchall()}


async def _batch_and_incremental(root: Path, files: list[Path], incr_db: Path):
    batch = await Workspace.create(root, default_db_path(root))
    await batch.full_index()
    incr = await Workspace.create(root, incr_db)
    for f in files:
        await incr.ensure_fresh(f, semantic=True)
    return batch, incr


async def test_single_file_incremental_equals_batch_exactly(tmp_path):
    # Arrange: one self-contained module with intra-file calls a -> b -> c
    root = tmp_path / "proj"
    root.mkdir()
    mod = root / "mod.py"
    mod.write_text(
        "def c():\n    return 1\n\n\ndef b():\n    return c()\n\n\ndef a():\n    return b()\n"
    )
    batch, incr = await _batch_and_incremental(root, [mod], tmp_path / "incr.db")

    try:
        # Act
        diff = diff_graphs(await _file_node_graph(batch.store), await _file_node_graph(incr.store))
        # Assert: identical nodes AND identical edges
        assert (diff.added_nodes, diff.removed_nodes, diff.changed_nodes) == ([], [], [])
        assert await _file_owned_edges(batch.store) == await _file_owned_edges(incr.store)
    finally:
        await batch.store.close()
        await incr.store.close()


async def test_multifile_incremental_matches_batch_nodes(py_project: Path, tmp_path):
    # Across files, the file-bearing node set must match (guards path normalization
    # and that no file's symbols are dropped on either path).
    files = sorted((py_project / "pkg").glob("*.py"))
    batch, incr = await _batch_and_incremental(py_project, files, tmp_path / "incr.db")

    try:
        diff = diff_graphs(await _file_node_graph(batch.store), await _file_node_graph(incr.store))
        assert (diff.added_nodes, diff.removed_nodes, diff.changed_nodes) == ([], [], [])
    finally:
        await batch.store.close()
        await incr.store.close()
