"""Integration tests for Workspace: full index, freshness, pruning, cwd safety."""

from __future__ import annotations

from pathlib import Path

import pytest

from graphlens_mcp.indexer.workspace import Workspace, default_db_path

pytestmark = [pytest.mark.integration, pytest.mark.workspace]


async def _indexed(root: Path) -> Workspace:
    ws = await Workspace.create(root, default_db_path(root))
    await ws.full_index()
    return ws


async def _func_id(ws: Workspace, name: str) -> str:
    hits = await ws.store.search_symbols(name)
    return next(h["id"] for h in hits if h["name"] == name and h["kind"] == "function")


async def test_full_index_records_absolute_paths_only(py_project: Path):
    ws = await _indexed(py_project)
    try:
        files = await ws.store.list_files()
        assert files, "expected indexed files"
        assert all(Path(f["path"]).is_absolute() for f in files)
    finally:
        await ws.store.close()


async def test_callers_span_files(py_project: Path):
    # b.use and a.main both call a.helper across file boundaries
    ws = await _indexed(py_project)
    try:
        helper_id = await _func_id(ws, "helper")
        callers = {n["name"] for n in await ws.store.get_callers(helper_id)}
        assert {"main", "use"} <= callers
    finally:
        await ws.store.close()


async def test_deleted_file_is_pruned_on_access(py_project: Path):
    ws = await _indexed(py_project)
    try:
        b_py = py_project / "pkg" / "b.py"
        b_abs = str(b_py.resolve())
        assert await ws.store.get_nodes_in_file(b_abs)  # present before delete

        b_py.unlink()
        await ws.ensure_fresh(b_py)

        assert await ws.store.get_nodes_in_file(b_abs) == []
        assert all(h["name"] != "use" for h in await ws.store.search_symbols("use"))
    finally:
        await ws.store.close()


async def test_indexing_from_a_foreign_cwd_keeps_paths_absolute(
    py_project: Path, tmp_path, monkeypatch
):
    # Regression: adapters emit some relative paths; indexing off-root must not drop
    # files or create phantom relative rows.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    ws = await _indexed(py_project)
    try:
        files = await ws.store.list_files()
        assert all(Path(f["path"]).is_absolute() for f in files)
        # all three modules survive (a.py, b.py, __init__.py)
        assert len(files) == 3
    finally:
        await ws.store.close()
