"""Agent registry primitives: a data-driven spec per MCP client + (de)register.

Each coding agent stores MCP server config differently — Claude Code and Cursor
use a ``mcpServers`` map, VS Code uses ``servers`` with a ``type`` field, configs
live at different paths and some are global. :class:`AgentSpec` captures those
differences so adding an agent is data, not code, and ``init``/``remove`` operate
on any spec uniformly.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import tomlkit
import tomlkit.exceptions

# Key under which we register our server in each agent's config.
SERVER_KEY = "graphlens"


@dataclass(frozen=True)
class AgentSpec:
    """Declarative description of how to (de)register graphlens in one agent."""

    name: str  # registry key / --agent value
    label: str  # human display name
    scope: str  # 'project' or 'global'
    servers_key: str  # top-level config key: 'mcpServers' | 'servers' | 'mcp_servers'
    stdio_type: bool  # emit {"type": "stdio", ...} in the entry (VS Code)
    path_fn: Callable[[Path], Path]  # config file location for a project root
    detect_fn: Callable[[Path], bool]  # is this agent plausibly used here?
    fmt: str = "json"  # config file format: 'json' | 'toml' (Codex)
    install_skill: Callable[[Path], Path | None] | None = None

    def config_path(self, project_root: Path) -> Path:
        """Return this agent's MCP config file location for *project_root*."""
        return self.path_fn(project_root.resolve())

    def detect(self, project_root: Path) -> bool:
        """Return True if this agent looks like it is used for *project_root*."""
        try:
            return bool(self.detect_fn(project_root.resolve()))
        except OSError:
            return False


def _server_entry(spec: AgentSpec, project_root: Path, db_path: Path) -> dict:
    # Pass absolute --db and --root so the entry does not depend on the agent's
    # working directory (clients launch the server with varying cwd).
    entry: dict = {
        "command": "graphlens-mcp",
        "args": ["serve", "--db", str(db_path), "--root", str(project_root)],
    }
    if spec.stdio_type:
        return {"type": "stdio", **entry}
    return entry


def configure(spec: AgentSpec, project_root: Path, db_path: Path) -> Path:
    """Write (or update) the graphlens MCP entry in *spec*'s config. Idempotent."""
    project_root = project_root.resolve()
    path = spec.config_path(project_root)
    entry = _server_entry(spec, project_root, db_path.resolve())

    if spec.fmt == "toml":
        doc = _load_toml(path)
        table = doc.get(spec.servers_key)
        if not isinstance(table, dict):
            table = tomlkit.table()
            doc[spec.servers_key] = table
        table[SERVER_KEY] = entry
        _atomic_write_text(path, tomlkit.dumps(doc))
    else:
        cfg = _load_json(path)
        cfg.setdefault(spec.servers_key, {})[SERVER_KEY] = entry
        _atomic_write_text(path, json.dumps(cfg, indent=2) + "\n")
    return path


def deregister(spec: AgentSpec, project_root: Path) -> bool:
    """Remove the graphlens entry from *spec*'s config. Returns True if removed."""
    path = spec.config_path(project_root.resolve())
    if not path.exists():
        return False

    if spec.fmt == "toml":
        doc = _load_toml(path)
        table = doc.get(spec.servers_key)
        if not isinstance(table, dict) or SERVER_KEY not in table:
            return False
        del table[SERVER_KEY]
        if len(table) == 0:
            del doc[spec.servers_key]
        _atomic_write_text(path, tomlkit.dumps(doc))
        return True

    cfg = _load_json(path)
    servers = cfg.get(spec.servers_key)
    if not isinstance(servers, dict) or SERVER_KEY not in servers:
        return False
    del servers[SERVER_KEY]
    if not servers:
        cfg.pop(spec.servers_key, None)
    _atomic_write_text(path, json.dumps(cfg, indent=2) + "\n")
    return True


def _load_json(path: Path) -> dict:
    if path.exists():
        try:
            data = json.loads(path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def _load_toml(path: Path) -> tomlkit.TOMLDocument:
    if path.exists():
        try:
            return tomlkit.parse(path.read_text())
        except (OSError, tomlkit.exceptions.TOMLKitError):
            return tomlkit.document()
    return tomlkit.document()


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".graphlens-", suffix=".tmp")
    tmp_path = Path(tmp)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
