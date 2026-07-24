"""
Arms — the three code-context MCP servers under test. Pure config + command builders.

Every arm exposes exactly one MCP server to an otherwise-identical agent. The
agent gets *no other tools* (no built-in filesystem, no web) so the only variable
is the quality of the code-context surface.

Each arm provides:
  - `serve(project)` -> ServeSpec(command, args, cwd, env) for an stdio MCP server
    that pydantic-ai launches as a toolset and keeps warm for the whole
    (project, arm) sweep — so a heavy index loads once, not per task.
  - `index(project)` -> optional pre-build command (measured into index_costs.jsonl),
    or None if the server indexes lazily / needs no index.

Transport note: unlike the reference (which fought Claude Code's sub-second MCP
startup race with a warm mcp-proxy over SSE), pydantic-ai holds the stdio process
open across all runs, so plain stdio is fine even for the slow-loading arms.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from bench.config import ROOT, STORES_DIR

if TYPE_CHECKING:
    from collections.abc import Callable

    from bench.projects import Project

# The graphlens-mcp repo this benchmark lives inside — we test THIS working copy,
# not a globally-installed version, by running it through the repo's uv project.
GRAPHLENS_REPO = ROOT.parent


@dataclass(frozen=True)
class ServeSpec:
    command: str
    args: list[str]
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Arm:
    name: str  # slug for filenames/labels, e.g. "graphlens"
    tool_prefix: str  # MCP tool namespace -> tools surface as <prefix>_<tool>
    _serve: Callable[[Project], ServeSpec] | None
    _index: Callable[[Project], ServeSpec | None]
    # Optional per-arm system-prompt addendum. Some tools (semble) require the
    # caller to pass the repo path on every call; this tells the agent the path.
    _instructions: Callable[[Project], str] | None = None
    # Control arm: no MCP tools at all — the model answers from parametric memory.
    # Its lift below each real arm is the tool's true contribution, robust to the
    # training-data contamination that inflates every arm equally.
    is_control: bool = False

    def serve(self, project: Project) -> ServeSpec:
        if self._serve is None:
            msg = f"arm {self.name!r} is a control arm with no server"
            raise RuntimeError(msg)
        return self._serve(project)

    def index(self, project: Project) -> ServeSpec | None:
        return self._index(project)

    def instructions(self, project: Project) -> str:
        return self._instructions(project) if self._instructions else ""


# --- graphlens (this repo's working copy) ----------------------------------


def _graphlens_env(p: Project) -> dict[str, str]:
    """
    Full environment for a graphlens process, isolated to this project.

    graphlens keeps one machine-wide store (registry + graph + vectors) under
    platformdirs' user data dir, and its tools take an optional ``project``
    argument that may be omitted only when exactly ONE project is indexed.
    Pointing XDG_DATA_HOME at a per-project directory gives each benchmark
    target its own store, so the agent never has to know a project id and one
    target's graph can't leak into another's answers.

    The env is passed whole (not as an overlay): the MCP stdio transport
    replaces the child environment when given one, so dropping PATH here would
    hide gopls / rust-analyzer and silently downgrade the index.
    """
    store = STORES_DIR / p.key
    store.mkdir(parents=True, exist_ok=True)
    return {**os.environ, "XDG_DATA_HOME": str(store)}


def _graphlens_serve(p: Project) -> ServeSpec:
    # Run the repo's current code + deps via uv so we benchmark THIS branch.
    # --no-sync: the environment is prepared once, up front; re-resolving it per
    # spawn only adds startup latency (and fails offline / behind a flaky proxy).
    return ServeSpec(
        command="uv",
        args=[
            "run",
            "--project",
            str(GRAPHLENS_REPO),
            "--no-sync",
            "graphlens-mcp",
        ],
        cwd=str(p.analyze_path),
        env=_graphlens_env(p),
    )


def _graphlens_index(p: Project) -> ServeSpec:
    # Build the graph up front (measured) so the served process opens a warm
    # store instead of indexing during the agent's first turn. There is no
    # index CLI — indexing is the `index` MCP tool — so this drives it through
    # an in-process client (scripts/gl_index.py).
    return ServeSpec(
        command="uv",
        args=[
            "run",
            "--project",
            str(GRAPHLENS_REPO),
            "--no-sync",
            "python",
            str(ROOT / "scripts" / "gl_index.py"),
            str(p.analyze_path),
        ],
        cwd=str(GRAPHLENS_REPO),
        env=_graphlens_env(p),
    )


# --- semble (MinishLab/semble) ---------------------------------------------


def _semble_serve(p: Project) -> ServeSpec:
    # Bare `semble` (with the [mcp] extra) starts the stdio MCP server. semble's
    # tools require a `repo` argument on every call (no cwd default), so the agent
    # is told the absolute path via _semble_instructions below.
    return ServeSpec(
        command="uvx",
        args=["--from", "semble[mcp]", "semble"],
        cwd=str(p.analyze_path),
    )


def _semble_index(_p: Project) -> ServeSpec | None:
    # Semble indexes lazily on the first search (embeds the repo) and caches it for
    # the warm session — so the embedding cost lands on the first query, not a
    # separate step. Recorded implicitly in that run's wall time.
    return None


def _semble_instructions(p: Project) -> str:
    return (
        f"The repository under test is at the absolute path '{p.analyze_path}'. "
        f"Your tool is a SEMANTIC code-search tool. You MUST call `search` "
        f"(always passing repo='{p.analyze_path}') to locate the answer — never "
        f"answer from memory. Each `search` result includes the repo-relative "
        f"FILE PATH and LINE NUMBER of the matching code; read the answer "
        f"directly from the most relevant result. Use at most TWO or THREE "
        f"searches total, then answer — do not keep searching once a relevant "
        f"result has been returned."
    )


# --- codegraph -------------------------------------------------------------


def _codegraph_serve(p: Project) -> ServeSpec:
    return ServeSpec(
        command="codegraph",
        args=["serve", "--mcp", "--path", str(p.analyze_path), "--no-watch"],
        cwd=str(p.analyze_path),
    )


def _codegraph_index(p: Project) -> ServeSpec:
    # `codegraph init <path>` builds the initial index (indexing runs by default).
    # MCP `serve` exposes 0 tools until this index exists, so it is mandatory.
    return ServeSpec(
        command="codegraph",
        args=["init", "-f", str(p.analyze_path)],
    )


ARMS: dict[str, Arm] = {
    "graphlens": Arm(
        "graphlens", "graphlens", _graphlens_serve, _graphlens_index
    ),
    "semble": Arm(
        "semble", "semble", _semble_serve, _semble_index, _semble_instructions
    ),
    # codegraph's own MCP server exposes only `codegraph_explore` by default
    # (DEFAULT_MCP_TOOLS = {'explore'} in its source) — this measures that
    # out-of-the-box default surface, matching how a real user installs it.
    # (An earlier "codegraph-full" arm force-enabled all 8 tools via
    # CODEGRAPH_MCP_TOOLS to test whether tool count explained codegraph's
    # edge on weak models — it didn't: 8 tools scored equal-or-better than 4,
    # so that arm was dropped as not representative of a default install.)
    "codegraph": Arm(
        "codegraph", "codegraph", _codegraph_serve, _codegraph_index
    ),
    # Control: no tools. Quantifies how much each real arm adds over pure model
    # memory — the contamination-robust denominator.
    "none": Arm("none", "none", None, lambda _p: None, is_control=True),
}


def arms_for(keys: list[str] | None = None) -> list[Arm]:
    if not keys:
        return list(ARMS.values())
    return [ARMS[k] for k in keys]


SYSTEM_PROMPT = (
    "You are a precise code-navigation agent. You are given tools that let you "
    "explore a specific codebase. You MUST use these tools to locate the answer in "
    "the actual source code — never answer from prior knowledge or memory, and never "
    "guess. Investigate until you are certain, then respond with ONLY the exact "
    "symbol name(s) or repo-relative file path(s) requested — no prose, no explanation. "
    "If the question asks for several items, list each on its own line."
)

# Control arm: no tools available, so it must answer from memory. Same strict
# output contract so its answers grade identically to the tool-using arms.
CONTROL_SYSTEM_PROMPT = (
    "You are a code expert answering questions about a specific open-source "
    "codebase from your own knowledge — you have NO tools available. Give your best "
    "answer. Respond with ONLY the exact symbol name(s) or repo-relative file "
    "path(s) requested — no prose, no explanation. If the question asks for several "
    "items, list each on its own line."
)
