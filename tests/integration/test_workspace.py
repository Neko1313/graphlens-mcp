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
    # On-access correctness: an edit (by a human or agent) is reflected on the
    # next query — the changed file is re-indexed (full) when next touched.
    ws = await _indexed(py_project)
    try:
        a_py = py_project / "pkg" / "a.py"
        a_abs = str(a_py.resolve())
        before = {n["name"] for n in await ws.store.get_nodes_in_file(a_abs)}
        assert "freshly_added" not in before

        a_py.write_text(
            a_py.read_text() + "\n\ndef freshly_added():\n    return 1\n"
        )
        await ws.ensure_fresh(a_py)

        after = {n["name"] for n in await ws.store.get_nodes_in_file(a_abs)}
        assert "freshly_added" in after
    finally:
        await ws.store.close()


async def test_changed_file_reindexes_connected_importers(py_project: Path):
    # b.py imports a.py and b.use calls a.helper. Editing a.py and re-indexing
    # it must rebuild the connected set (a + its importer b) together, so the
    # cross-file caller edge b.use -> a.helper is preserved (a lone single-file
    # re-index of a could not see b and would drop it).
    ws = await _indexed(py_project)
    try:
        a_py = py_project / "pkg" / "a.py"
        helper_id = await _func_id(ws, "helper")
        before = {n["name"] for n in await ws.store.get_callers(helper_id)}
        assert {"main", "use"} <= before

        a_py.write_text(a_py.read_text().replace("x + 1", "x + 2"))
        await ws.ensure_fresh(a_py)

        after = {n["name"] for n in await ws.store.get_callers(helper_id)}
        assert {"main", "use"} <= after  # b.use edge survived the rebuild
    finally:
        await ws.store.close()


async def test_deleting_a_dependency_prunes_and_refreshes_importers(
    py_project: Path,
):
    # Deleting a.py prunes its nodes and re-indexes b.py (its importer), so the
    # graph no longer reports a.helper at all.
    ws = await _indexed(py_project)
    try:
        a_py = py_project / "pkg" / "a.py"
        a_abs = str(a_py.resolve())
        assert await ws.store.get_nodes_in_file(a_abs)

        a_py.unlink()
        await ws.reindex_connected({a_abs})

        # a's nodes (the real helper definition) are pruned; any "helper" left
        # in search is b.py's now-dangling import stub, not a definition in a.py.
        assert await ws.store.get_nodes_in_file(a_abs) == []
        assert all(
            h.get("file_path") != a_abs
            for h in await ws.store.search_symbols("helper")
        )
    finally:
        await ws.store.close()


async def test_watcher_reindexes_edited_file(py_project: Path, monkeypatch):
    # End-to-end: the watcher (the single freshness mechanism) re-indexes a
    # file edited on disk without any tool query. Force polling so the test is
    # deterministic regardless of the sandbox's inotify support.
    monkeypatch.setenv("WATCHFILES_FORCE_POLLING", "true")
    ws = await _indexed(py_project)
    try:
        a_py = py_project / "pkg" / "a.py"
        a_abs = str(a_py.resolve())
        edited = a_py.read_text() + "\n\ndef watched_add():\n    return 1\n"
        ws.start_watching()
        await asyncio.sleep(1.0)  # let the watcher establish its baseline

        names: set[str] = set()
        for _ in range(75):  # ~15s budget
            # Re-write each iteration so a poll cycle always sees an mtime
            # change, regardless of when the watcher armed.
            a_py.write_text(edited)
            await asyncio.sleep(0.2)
            names = {
                n["name"] for n in await ws.store.get_nodes_in_file(a_abs)
            }
            if "watched_add" in names:
                break
        assert "watched_add" in names
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
