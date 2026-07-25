"""
Setup phase: clone targets, verify arm binaries, build & time indices.

Kept apart from the run loop so the upfront, amortizable cost (cloning + static
indexing) is measured once and recorded in data/index_costs.jsonl — a different
currency from per-query token cost, and never blended into it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from typing import TYPE_CHECKING

from bench.config import DATA_DIR, TARGETS_DIR

if TYPE_CHECKING:
    from bench.arms import Arm
    from bench.projects import Project


def clone(project: Project, *, force: bool = False) -> None:
    """Shallow-clone the target at its pinned tag (idempotent)."""
    path = project.path
    if path.exists() and not force:
        return
    if path.exists():
        shutil.rmtree(path)
    TARGETS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"  cloning {project.repo}@{project.tag} -> {path} ...", flush=True)
    subprocess.run(
        [
            "git",
            "clone",
            "--depth=1",
            "--branch",
            project.tag,
            project.clone_url,
            str(path),
        ],
        check=True,
    )


def arm_available(arm: Arm, project: Project) -> tuple[bool, str]:
    """Is this arm's server binary runnable? Returns (ok, reason)."""
    if arm.is_control:
        return True, ""  # control arm needs no binary
    cmd = arm.serve(project).command
    if shutil.which(cmd) is None:
        return False, f"binary '{cmd}' not on PATH"
    return True, ""


def _index_stats(stdout: str) -> dict | None:
    """Return the JSON summary an index step printed on its last line."""
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()  # noqa: PLW2901
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                return None
    return None


def build_index(arm: Arm, project: Project) -> dict | None:
    """Run the arm's pre-index step (if any) and time it. Returns an index_costs row."""
    spec = arm.index(project)
    if spec is None:
        return None
    print(f"  index {arm.name} / {project.key} ...", flush=True)
    t0 = time.perf_counter()
    proc = subprocess.run(
        [spec.command, *spec.args],
        capture_output=True,
        text=True,
        check=False,
        cwd=spec.cwd,
        # An arm that scopes its store per project (graphlens, via XDG_DATA_HOME)
        # passes a complete environment; None means "inherit ours".
        env=spec.env or None,
    )
    wall = time.perf_counter() - t0
    row = {
        "project": project.key,
        "arm": arm.name,
        "wall_s": round(wall, 3),
        "ok": proc.returncode == 0,
    }
    stats = _index_stats(proc.stdout)
    if stats:
        # Node/edge counts and per-language resolver status: the cheapest way to
        # catch an index that silently came out *degraded* (missing gopls) before
        # a whole sweep is spent measuring a half-built graph.
        row["stats"] = stats
    if proc.returncode != 0:
        row["stderr"] = proc.stderr[-500:]
        print(
            f"    WARN index failed ({arm.name}/{project.key}): {proc.stderr[-200:]}"
        )
    return row


def record_index_cost(row: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with (DATA_DIR / "index_costs.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


# Language servers the graph arms need for full (non-degraded) resolution.
_LANG_SERVER = {"go": "gopls", "rust": "rust-analyzer"}


def toolchain_preflight(projects: list[Project], arms: list[Arm]) -> list[str]:
    """
    Warn when a graph arm will run *degraded* for lack of a language server.

    graphlens/codegraph resolve Go via gopls and Rust via rust-analyzer; without
    them those languages parse but cross-file calls aren't fully resolved — which
    would unfairly handicap the graph arms. Returns a list of warning strings.
    """
    graph_arms = {a.name for a in arms} & {"graphlens", "codegraph"}
    if not graph_arms:
        return []
    warnings: list[str] = []
    for project in projects:
        for lang in project.languages:
            srv = _LANG_SERVER.get(lang)
            if srv and shutil.which(srv) is None:
                warnings.append(
                    f"{srv} missing -> {sorted(graph_arms)} run DEGRADED on "
                    f"{lang} ({project.key}). Install: "
                    + (
                        "go install golang.org/x/tools/gopls@latest"
                        if lang == "go"
                        else "rustup component add rust-analyzer"
                    )
                )
    return warnings


def tool_version(cmd: str, *args: str) -> str:
    if shutil.which(cmd) is None:
        return ""
    try:
        out = subprocess.run(
            [cmd, *args],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        text = (out.stdout or out.stderr).strip()
        first = text.splitlines()[0] if text else ""
        # Tools without a --version flag echo usage/errors — don't record that noise.
        if any(
            tok in first
            for tok in ("Usage:", "usage:", "Error", "ENOENT", "No such")
        ):
            return ""
        return first
    except Exception:
        return ""
