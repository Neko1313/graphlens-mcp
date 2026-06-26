"""Integration tests for Workspace: full index, freshness, pruning, cwd safety."""

from __future__ import annotations

import asyncio
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
    return next(
        h["id"] for h in hits if h["name"] == name and h["kind"] == "function"
    )


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
        assert all(
            h["name"] != "use" for h in await ws.store.search_symbols("use")
        )
    finally:
        await ws.store.close()


async def test_edit_is_picked_up_on_next_query(py_project: Path):
    # On-access freshness: an edit (by a human or agent) is reflected on the next query,
    # with no file watcher — the changed file is re-indexed when it is next touched.
    ws = await _indexed(py_project)
    try:
        a_py = py_project / "pkg" / "a.py"
        a_abs = str(a_py.resolve())
        before = {n["name"] for n in await ws.store.get_nodes_in_file(a_abs)}
        assert "freshly_added" not in before

        a_py.write_text(
            a_py.read_text() + "\n\ndef freshly_added():\n    return 1\n"
        )
        await ws.ensure_fresh(a_py, semantic=True)

        after = {n["name"] for n in await ws.store.get_nodes_in_file(a_abs)}
        assert "freshly_added" in after
    finally:
        await ws.store.close()


async def test_dependency_change_degrades_importer(py_project: Path):
    # Transitive freshness: b.py imports a.py. Editing a.py and then querying b.py
    # (semantic) must report 'degraded', not a false 'ok' — single-file analysis cannot
    # re-resolve b's cross-file calls into a's new definition until a full reindex.
    ws = await _indexed(py_project)
    try:
        a_py = py_project / "pkg" / "a.py"
        b_py = py_project / "pkg" / "b.py"
        # b is fully resolved before the dependency changes
        assert await ws.ensure_fresh(b_py, semantic=True) == "ok"

        a_py.write_text(a_py.read_text().replace("x + 1", "x + 999"))
        # b itself is unchanged on disk, but its dependency a.py changed
        assert await ws.ensure_fresh(b_py, semantic=True) == "degraded"
    finally:
        await ws.store.close()


async def test_background_refresh_picks_up_edits_without_a_query(
    py_project: Path,
):
    # The background sweep must re-index a file edited on disk even though no
    # tool ever queries it (get_nodes_in_file is a plain read, not ensure_fresh).
    ws = await _indexed(py_project)
    try:
        a_py = py_project / "pkg" / "a.py"
        a_abs = str(a_py.resolve())
        a_py.write_text(
            a_py.read_text() + "\n\ndef bg_added():\n    return 1\n"
        )

        ws.start_background_refresh(interval=0.05)
        names: set[str] = set()
        for _ in range(100):
            await asyncio.sleep(0.05)
            names = {
                n["name"] for n in await ws.store.get_nodes_in_file(a_abs)
            }
            if "bg_added" in names:
                break
        assert "bg_added" in names
    finally:
        await ws.close()


async def test_close_is_idempotent(py_project: Path):
    ws = await _indexed(py_project)
    await ws.close()
    # A second close must not raise (best-effort resolver shutdown + already-closed store)
    await ws.close()


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
