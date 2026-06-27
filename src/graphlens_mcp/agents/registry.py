"""
The set of supported coding agents.

Only agents whose MCP config format and location are known are included, so we
never write a config an agent cannot read. Adding another agent is a single
:class:`AgentSpec` entry. Codex uses TOML (``~/.codex/config.toml``); the
rest use JSON. Zed and Cline use distinct schemas/locations and are not
registered yet.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from graphlens_mcp.agents.base import AgentSpec

_SKILL_SRC = (
    Path(__file__).parent.parent
    / "skills"
    / "graphlens-navigation"
    / "SKILL.md"
)


def _install_claude_skill(_project_root: Path) -> Path | None:
    if not _SKILL_SRC.exists():
        return None
    dest_dir = Path.home() / ".claude" / "skills" / "graphlens-navigation"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "SKILL.md"
    shutil.copy2(_SKILL_SRC, dest)
    return dest


REGISTRY: dict[str, AgentSpec] = {
    "claude_code": AgentSpec(
        name="claude_code",
        label="Claude Code",
        scope="project",
        servers_key="mcpServers",
        stdio_type=False,
        path_fn=lambda r: r / ".mcp.json",
        detect_fn=lambda r: (
            (r / ".claude").is_dir()
            or (r / ".mcp.json").exists()
            or (r / "CLAUDE.md").exists()
        ),
        install_skill=_install_claude_skill,
    ),
    "cursor": AgentSpec(
        name="cursor",
        label="Cursor",
        scope="project",
        servers_key="mcpServers",
        stdio_type=False,
        path_fn=lambda r: r / ".cursor" / "mcp.json",
        detect_fn=lambda r: (r / ".cursor").is_dir(),
    ),
    "windsurf": AgentSpec(
        name="windsurf",
        label="Windsurf",
        scope="global",
        servers_key="mcpServers",
        stdio_type=False,
        path_fn=lambda _r: (
            Path.home() / ".codeium" / "windsurf" / "mcp_config.json"
        ),
        detect_fn=lambda _r: (Path.home() / ".codeium" / "windsurf").is_dir(),
    ),
    "vscode": AgentSpec(
        name="vscode",
        label="VS Code (Copilot)",
        scope="project",
        servers_key="servers",
        stdio_type=True,
        path_fn=lambda r: r / ".vscode" / "mcp.json",
        detect_fn=lambda r: (r / ".vscode").is_dir(),
    ),
    "codex": AgentSpec(
        name="codex",
        label="Codex CLI",
        scope="global",
        servers_key="mcp_servers",
        stdio_type=False,
        fmt="toml",
        path_fn=lambda _r: Path.home() / ".codex" / "config.toml",
        detect_fn=lambda _r: (Path.home() / ".codex").is_dir(),
    ),
}
