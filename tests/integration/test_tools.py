"""Integration tests for the MCP tool layer over a real indexed project."""

from __future__ import annotations

from pathlib import Path

import pytest

from graphlens_mcp.indexer.workspace import Workspace, default_db_path
from graphlens_mcp.server.mcp_server import create_mcp
from graphlens_mcp.server.tools import (
    tool_get_callers,
    tool_get_file_structure,
    tool_search_symbols,
)

pytestmark = [pytest.mark.integration, pytest.mark.tools]


async def _workspace(root: Path) -> Workspace:
    ws = await Workspace.create(root, default_db_path(root))
    await ws.full_index()
    return ws


async def test_search_returns_typed_result_with_status(py_project: Path):
    ws = await _workspace(py_project)
    try:
        result = await tool_search_symbols(ws.store, "helper")
        assert result.error is None
        assert any(n.name == "helper" for n in result.nodes)
        assert result.resolver_status in {"ok", "degraded"}
    finally:
        await ws.close()


async def test_callers_tool_reports_impact_across_files(py_project: Path):
    ws = await _workspace(py_project)
    try:
        hits = await tool_search_symbols(ws.store, "helper")
        helper = next(
            n
            for n in hits.nodes
            if n.name == "helper" and n.kind == "function"
        )
        result = await tool_get_callers(ws.store, ws, helper.id)
        assert {n.name for n in result.nodes} >= {"main", "use"}
    finally:
        await ws.close()


async def test_file_structure_accepts_a_project_relative_path(
    py_project: Path, monkeypatch
):
    # The agent passes a relative path; it must resolve against the project root,
    # not the server's cwd.
    ws = await _workspace(py_project)
    monkeypatch.chdir(Path("/"))
    try:
        result = await tool_get_file_structure(ws.store, ws, "pkg/a.py")
        assert result.error is None
        assert any(n.name == "helper" for n in result.nodes)
    finally:
        await ws.close()


async def test_create_mcp_registers_all_tools_with_output_schemas(
    py_project: Path,
):
    # Regression: with `from __future__ import annotations`, FastMCP evaluates
    # each tool's return annotation at registration time to build its output
    # schema. If the response models are imported only under TYPE_CHECKING, that
    # eval raises NameError here and the server never starts — the client then
    # reports it "cannot connect". Building create_mcp must succeed and expose
    # every tool with a non-trivial schema.
    ws = await _workspace(py_project)
    try:
        mcp = create_mcp(ws.store, ws)
        tools = await mcp.list_tools()
        names = {t.name for t in tools}
        assert names == {
            "search_symbols",
            "get_node_info",
            "get_file_structure",
            "get_callees",
            "get_callers",
            "get_neighbors",
            "find_references",
            "get_cross_language_calls",
            # Semantic layer ([semantic] extra): registered unconditionally so
            # the schema is always advertised; they report available=false at
            # call time when the extra/model is missing.
            "search_code",
            "search_semantic",
            "find_related",
            "list_clusters",
            "get_cluster",
        }
        # The output schema is what eval-of-annotation produces; it must exist
        # and describe the typed envelope rather than being absent/empty.
        search = next(t for t in tools if t.name == "search_symbols")
        assert search.output_schema
    finally:
        await ws.close()


async def test_unknown_node_returns_error_envelope(py_project: Path):
    ws = await _workspace(py_project)
    try:
        result = await tool_get_callers(ws.store, ws, "does-not-exist")
        assert result.error is not None
        assert result.nodes == []
    finally:
        await ws.close()
