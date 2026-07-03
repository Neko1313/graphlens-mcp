"""
Benchmark orchestrator.

    uv run main.py                      # full matrix: all projects × arms × models
    uv run main.py --setup-only         # clone + index only (no agent runs)
    uv run main.py --projects gin hono --arms graphlens codegraph
    uv run main.py --models qwen3-8b --only where_defined impact_set --seeds 1

Matrix = projects × arms × models × tasks × seeds. Results stream to
data/<project>__<arm>__<model>.jsonl, one row per run. Re-running resumes:
already-completed (task_id, seed) rows are skipped, so a crash or a rate-limit
pause loses no completed work.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
from typing import TYPE_CHECKING

from bench import config, setup
from bench.arms import ARMS, Arm, arms_for
from bench.dataset import Task, load_tasks
from bench.models import MODELS
from bench.projects import PROJECTS, Project, projects_for
from bench.runner import ArmSession, RunOutput, make_pricing
from bench.scoring import score

if TYPE_CHECKING:
    import pathlib


def result_path(project: Project, arm: Arm, model_key: str) -> pathlib.Path:
    return config.DATA_DIR / f"{project.key}__{arm.name}__{model_key}.jsonl"


def load_done(path: pathlib.Path) -> set[tuple[str, int]]:
    """(task_id, seed) pairs already recorded with a non-error answer."""
    done: set[tuple[str, int]] = set()
    if not path.exists():
        return done
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not str(r.get("answer", "")).startswith("__"):
            done.add((r["task_id"], int(r.get("seed", 0))))
    return done


def row_from(
    project: Project,
    arm: Arm,
    model_key: str,
    seed: int,
    task: Task,
    out: RunOutput,
) -> dict:
    return {
        "project": project.key,
        "arm": arm.name,
        "model": model_key,
        "seed": seed,
        "task_id": task.id,
        "kind": task.kind,
        "regime": task.regime,
        "prompt": task.prompt,
        "expected": task.expected,
        "answer": out.answer,
        "accuracy": round(score(out.answer, task.expected), 4),
        "input_tokens": out.input_tokens,
        "output_tokens": out.output_tokens,
        "total_tokens": out.total_tokens,
        "cost_exact": out.cost_exact,
        "cost_table": round(out.cost_table, 6),
        "num_turns": out.num_turns,
        "n_tool_calls": out.n_tool_calls,
        "tool_names": out.tool_names,
        "tool_calls": out.tool_calls,
        "wall_s": round(out.wall_s, 3),
        "verified": task.verified,
        "gold_src": task.gold_src,
    }


async def _run_job(
    session: ArmSession,
    model_key: str,
    path: pathlib.Path,
    lock: asyncio.Lock,
    sem: asyncio.Semaphore,
    task: Task,
    seed: int,
) -> None:
    """Run one (task, seed), score it, and append the result row under lock."""
    async with sem:
        out = await session.run(model_key, task.prompt, seed=seed)
    project, arm = session.project, session.arm
    row = row_from(project, arm, model_key, seed, task, out)
    async with lock:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    flag = "OK " if not out.is_error else "ERR"
    print(
        f"  [{project.key}/{arm.name}/{model_key}] {task.id} s{seed} "
        f"{flag} acc={row['accuracy']} calls={out.n_tool_calls} "
        f"tok={out.total_tokens} {out.wall_s:.1f}s",
        flush=True,
    )


async def run_project(
    project: Project,
    arms: list[Arm],
    model_keys: list[str],
    tasks: list[Task],
    pricing: dict,
    seeds: int,
    sem: asyncio.Semaphore,
) -> None:
    """
    Run a whole project's matrix through one shared concurrency pool.

    Every arm's MCP server is started and held warm up front, then the full
    (arm × model × task × seed) job list runs through a single semaphore — so a
    slow arm (semble looping) never blocks the others.
    """
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    async with contextlib.AsyncExitStack() as stack:
        sessions: dict[str, ArmSession] = {}
        for arm in arms:
            try:
                sessions[arm.name] = await stack.enter_async_context(
                    ArmSession(arm, project, pricing)
                )
            except Exception as exc:
                print(
                    f"  SKIP {arm.name}/{project.key}: failed to start ({exc!r})"
                )

        locks: dict[pathlib.Path, asyncio.Lock] = {}
        jobs: list[tuple] = []
        for arm in arms:
            session = sessions.get(arm.name)
            if session is None:
                continue
            for model_key in model_keys:
                path = result_path(project, arm, model_key)
                locks[path] = asyncio.Lock()
                done = load_done(path)
                jobs += [
                    (session, model_key, path, task, seed)
                    for task in tasks
                    for seed in range(seeds)
                    if (task.id, seed) not in done
                ]

        if not jobs:
            print(f"  [{project.key}] all cells already complete")
            return
        print(
            f"  [{project.key}] {len(jobs)} runs queued across "
            f"{len(sessions)} arms × {len(model_keys)} models "
            f"(pool={config.CONCURRENCY})"
        )
        await asyncio.gather(
            *(
                _run_job(
                    session, model_key, path, locks[path], sem, task, seed
                )
                for session, model_key, path, task, seed in jobs
            )
        )


def write_manifest(
    projects: list[Project], arms: list[Arm], model_keys: list[str], seeds: int
) -> None:
    manifest = {
        "engine": "pydantic-ai + OpenRouter",
        "models": {k: MODELS[k] for k in model_keys},
        "arms": [a.name for a in arms],
        "projects": {
            p.key: {
                "repo": p.repo,
                "tag": p.tag,
                "languages": list(p.languages),
            }
            for p in projects
        },
        "n_seeds": seeds,
        "concurrency": config.CONCURRENCY,
        "max_turns": config.MAX_TURNS,
        "versions": {
            "codegraph": setup.tool_version("codegraph", "--version"),
            "semble": setup.tool_version("semble", "--version"),
            "graphlens-mcp": setup.tool_version("graphlens-mcp", "--version"),
        },
    }
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    (config.DATA_DIR / "run_manifest.json").write_text(
        json.dumps(manifest, indent=2)
    )


async def amain(args: argparse.Namespace) -> int:
    projects = projects_for(args.projects)
    arms = arms_for(args.arms)
    model_keys = args.models or list(MODELS)
    seeds = args.seeds if args.seeds is not None else config.N_SEEDS

    # --- setup: clone + index ------------------------------------------------
    print("== setup ==")
    for project in projects:
        setup.clone(project, force=args.reclone)
        for arm in arms:
            ok, reason = setup.arm_available(arm, project)
            if not ok:
                print(f"  SKIP index {arm.name}/{project.key}: {reason}")
                continue
            if args.reindex or not args.no_setup:
                row = setup.build_index(arm, project)
                if row:
                    setup.record_index_cost(row)
    write_manifest(projects, arms, model_keys, seeds)

    for w in setup.toolchain_preflight(projects, arms):
        print(f"  ⚠ TOOLCHAIN: {w}")

    if args.setup_only:
        print("setup-only: done")
        return 0

    # --- run -----------------------------------------------------------------
    config.openrouter_api_key()  # fail fast if missing before any work
    pricing = make_pricing()
    sem = asyncio.Semaphore(config.CONCURRENCY)
    print("== run ==")
    for project in projects:
        tasks = load_tasks(project)
        if args.only:
            tasks = [t for t in tasks if t.kind in set(args.only)]
        if not tasks:
            print(
                f"  no tasks for {project.key} (run scripts/build_gold.py first?)"
            )
            continue
        avail: list[Arm] = []
        for arm in arms:
            ok, reason = setup.arm_available(arm, project)
            if ok:
                avail.append(arm)
            else:
                print(f"  SKIP {arm.name}/{project.key}: {reason}")
        try:
            await run_project(
                project, avail, model_keys, tasks, pricing, seeds, sem
            )
        except Exception as exc:
            print(f"  ERROR {project.key}: {exc!r}")
    print("done")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="code-context MCP A/B benchmark")
    ap.add_argument(
        "--projects", nargs="*", choices=list(PROJECTS), help="default: all"
    )
    ap.add_argument(
        "--arms", nargs="*", choices=list(ARMS), help="default: all"
    )
    ap.add_argument(
        "--models", nargs="*", choices=list(MODELS), help="default: all"
    )
    ap.add_argument("--only", nargs="*", help="restrict to these task kinds")
    ap.add_argument(
        "--seeds",
        type=int,
        default=None,
        help=f"repeats (default {config.N_SEEDS})",
    )
    ap.add_argument(
        "--setup-only",
        action="store_true",
        help="clone + index, no agent runs",
    )
    ap.add_argument(
        "--no-setup",
        action="store_true",
        help="skip indexing (assume already built)",
    )
    ap.add_argument(
        "--reindex", action="store_true", help="force re-run of index steps"
    )
    ap.add_argument(
        "--reclone", action="store_true", help="delete + re-clone targets"
    )
    args = ap.parse_args()
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
