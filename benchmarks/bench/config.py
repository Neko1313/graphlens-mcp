"""
Paths, environment, and run-wide constants — no side effects on import.

Layout (all relative to the `benchmarks/` directory, `ROOT`):

    benchmarks/
      bench/            this package (config, arms, models, projects, runner, scoring, oracle)
      targets/          cloned target codebases (git-ignored, built by main.py)
      .stores/          per-target graphlens stores (git-ignored, built by setup)
      tasks/            *.jsonl task sets, one file per project
      data/             result JSONL (committed) + manifest + index costs
      scripts/          smoke_one, build_gold, run_all.sh, stop.sh
"""

from __future__ import annotations

import os
import pathlib

from dotenv import load_dotenv

ROOT = pathlib.Path(__file__).resolve().parent.parent
TARGETS_DIR = ROOT / "targets"
TASKS_DIR = ROOT / "tasks"
DATA_DIR = ROOT / "data"
# Per-project graphlens stores (graph + vectors + registry), one directory per
# target so each server sees exactly one indexed project. Rebuildable, so it is
# git-ignored — unlike data/, which holds the committed results.
STORES_DIR = ROOT / ".stores"

# Load benchmarks/.env if present (OPENROUTER_API_KEY lives there or in the shell env).
load_dotenv(ROOT / ".env")


def _ensure_toolchain_path() -> None:
    """
    Add per-user tool dirs to PATH so language servers stay discoverable.

    gopls / rust-analyzer live in ~/go/bin and ~/.cargo/bin, which a non-login
    shell often omits — and then the graph arms silently run *degraded*. Applies
    to shutil.which, the index subprocesses, and the spawned MCP servers.
    """
    extra = [
        pathlib.Path.home() / "go" / "bin",
        pathlib.Path.home() / ".cargo" / "bin",
        pathlib.Path.home() / ".local" / "bin",
    ]
    parts = os.environ.get("PATH", "").split(os.pathsep)
    have = set(parts)
    for d in extra:
        if d.is_dir() and str(d) not in have:
            parts.append(str(d))
    os.environ["PATH"] = os.pathsep.join(parts)


_ensure_toolchain_path()


def openrouter_api_key() -> str:
    """Return the OpenRouter key, or raise a clear error pointing to .env."""
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        msg = (
            "OPENROUTER_API_KEY is not set. Put it in benchmarks/.env "
            "(OPENROUTER_API_KEY=sk-or-...) or export it in your shell."
        )
        raise RuntimeError(msg)
    return key


# --- Run shape -------------------------------------------------------------

# Number of repeats per (project, arm, model, task). 2 keeps a minimal variance
# signal while holding cost down — at temperature 0 these models are *mostly*
# deterministic, so a second seed mainly catches the occasional flip. Bump via
# BENCH_SEEDS=5 for a higher-confidence (and ~2.5x pricier) run.
N_SEEDS = int(os.environ.get("BENCH_SEEDS", "2"))

# How many agent runs to drive concurrently. The pool spans the whole
# (arm x model x task x seed) matrix of a project — all arms' MCP servers are
# started up front and held warm — so a slow arm (semble looping) never
# blocks the others. 8 saturates a typical dev box.
CONCURRENCY = int(os.environ.get("BENCH_CONCURRENCY", "8"))

# Hard wall-clock ceiling per single run (seconds). A model that hangs or rambles
# without committing (seen on the no-tool control) is cut off and recorded as an
# error instead of stalling the whole sweep. Raised to 240s because the flash
# models take 25-75s PER turn under provider load, so at a high pool a few slow
# turns tripped the old 150s ceiling on runs that were converging fine — a
# harness/provider artifact, not a tool failure. Overridable via env.
RUN_TIMEOUT_S = float(os.environ.get("BENCH_RUN_TIMEOUT", "240"))

# Hard ceiling on agent turns so a run that spirals on grep fails fast instead
# of burning the budget. Reference used 50.
MAX_TURNS = int(os.environ.get("BENCH_MAX_TURNS", "40"))

# Per-tool-call timeout (seconds) for MCP tools — big graphs answer slower.
MCP_TOOL_TIMEOUT_S = float(os.environ.get("BENCH_MCP_TOOL_TIMEOUT", "60"))

# Token budget used by the TokenBudget evaluator (reported, not pass/fail-gating).
TOKEN_BUDGET = int(os.environ.get("BENCH_TOKEN_BUDGET", "8000"))

# Inject each MCP server's `instructions` into the agent? Off by default so the
# arm ranking stays apples-to-apples with the archived semble/codegraph runs
# (which had no instructions) and so weak-model tool-calling isn't destabilised
# by a wall of guidance. Set BENCH_INCLUDE_INSTRUCTIONS=1 to measure the
# realistic with-instructions deployment (real clients surface them).
INCLUDE_INSTRUCTIONS = os.environ.get("BENCH_INCLUDE_INSTRUCTIONS", "0") == "1"
