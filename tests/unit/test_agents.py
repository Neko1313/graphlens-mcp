"""Unit tests for the agent registry: JSON + TOML configure/deregister."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from graphlens_mcp.agents import REGISTRY, configure, deregister

pytestmark = [pytest.mark.unit, pytest.mark.agents]

DB = Path("/proj/.graphlens/graph.db")


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text())


async def test_configure_claude_code_writes_mcpservers_entry(tmp_path):
    # Act
    path = configure(REGISTRY["claude_code"], tmp_path, DB)
    # Assert
    cfg = _read_json(path)
    entry = cfg["mcpServers"]["graphlens"]
    assert entry["command"] == "graphlens-mcp"
    assert "serve" in entry["args"]
    assert str(DB) in entry["args"]


async def test_configure_preserves_unrelated_servers(tmp_path):
    # Arrange: a pre-existing, unrelated MCP server
    mcp = tmp_path / ".mcp.json"
    mcp.write_text(json.dumps({"mcpServers": {"other": {"command": "foo"}}}))
    # Act
    configure(REGISTRY["claude_code"], tmp_path, DB)
    # Assert: both entries coexist
    cfg = _read_json(mcp)
    assert set(cfg["mcpServers"]) == {"other", "graphlens"}


async def test_configure_vscode_uses_servers_key_with_stdio_type(tmp_path):
    # Act
    path = configure(REGISTRY["vscode"], tmp_path, DB)
    # Assert: VS Code uses `servers` (not `mcpServers`) and a stdio type
    cfg = _read_json(path)
    assert "mcpServers" not in cfg
    assert cfg["servers"]["graphlens"]["type"] == "stdio"


async def test_configure_codex_writes_toml_and_preserves_settings(tmp_path, monkeypatch):
    # Arrange: Codex config is global (~/.codex/config.toml) with existing settings
    monkeypatch.setenv("HOME", str(tmp_path))
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    (codex_dir / "config.toml").write_text('model = "o3"\n\n[mcp_servers.other]\ncommand = "foo"\n')
    # Act
    path = configure(REGISTRY["codex"], tmp_path, DB)
    # Assert
    doc = tomllib.loads(path.read_text())
    assert doc["model"] == "o3"  # unrelated setting preserved
    assert set(doc["mcp_servers"]) == {"other", "graphlens"}
    assert doc["mcp_servers"]["graphlens"]["command"] == "graphlens-mcp"


async def test_deregister_removes_only_our_entry(tmp_path):
    # Arrange
    mcp = tmp_path / ".mcp.json"
    mcp.write_text(json.dumps({"mcpServers": {"other": {"command": "foo"}}}))
    configure(REGISTRY["claude_code"], tmp_path, DB)
    # Act
    removed = deregister(REGISTRY["claude_code"], tmp_path)
    # Assert
    assert removed is True
    assert set(_read_json(mcp)["mcpServers"]) == {"other"}


async def test_deregister_returns_false_when_absent(tmp_path):
    # Arrange: a config with no graphlens entry
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"other": {"command": "foo"}}}))
    # Act / Assert
    assert deregister(REGISTRY["claude_code"], tmp_path) is False
