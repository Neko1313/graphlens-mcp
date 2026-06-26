"""Unit tests for the MCP tool response models and helpers."""

from __future__ import annotations

import pytest

from graphlens_mcp.server.models import (
    MAX_RESULTS,
    GraphResult,
    NodeRef,
    to_refs,
)

pytestmark = [pytest.mark.unit, pytest.mark.tools]


def _row(name: str) -> dict:
    return {
        "id": f"id-{name}",
        "kind": "function",
        "qualified_name": f"pkg.{name}",
        "name": name,
        "file_path": "/pkg/a.py",
        "span_json": "[1,0,2,0]",  # extra column must be ignored
    }


def test_node_ref_ignores_extra_columns():
    ref = NodeRef.from_row(_row("helper"))
    assert ref.name == "helper"
    assert not hasattr(ref, "span_json")


def test_to_refs_truncates_to_limit_and_flags_it():
    rows = [_row(str(i)) for i in range(5)]
    refs, truncated = to_refs(rows, limit=2)
    assert len(refs) == 2
    assert truncated is True


def test_to_refs_does_not_flag_when_within_limit():
    rows = [_row("a"), _row("b")]
    refs, truncated = to_refs(rows, limit=10)
    assert len(refs) == 2
    assert truncated is False


def test_to_refs_caps_at_max_results():
    rows = [_row(str(i)) for i in range(MAX_RESULTS + 50)]
    refs, truncated = to_refs(rows, limit=MAX_RESULTS + 50)
    assert len(refs) == MAX_RESULTS
    assert truncated is True


def test_graph_result_defaults_to_ok_status():
    assert GraphResult().resolver_status == "ok"
