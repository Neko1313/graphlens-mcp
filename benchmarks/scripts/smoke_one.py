"""Smoke test a single cell of the matrix.

# 1. Probe: connect each arm's MCP server to a project and list its tools.
#    No LLM, no API key, no cost — validates the MCP wiring in bench/arms.py.
uv run scripts/smoke_one.py --probe --project gin

# 2. Run: one (project, arm, model) on one task end-to-end (needs OPENROUTER_API_KEY).
uv run scripts/smoke_one.py --project gin --arm graphlens --model qwen3-8b
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from bench.arms import ARMS
from bench.dataset import load_tasks
from bench.models import MODELS, fetch_pricing
from bench.projects import PROJECTS
from bench.runner import ArmSession
from bench.scoring import score


async def probe(project_key: str, arm_keys: list[str]) -> int:
    from fastmcp import Client  # noqa: PLC0415 — lazy: only needed for --probe
    from pydantic_ai.mcp import StdioTransport  # noqa: PLC0415

    project = PROJECTS[project_key]
    if not project.path.exists():
        print(
            f"target {project_key} not cloned. Run: uv run main.py --setup-only --projects {project_key}"
        )
        return 1
    rc = 0
    for ak in arm_keys:
        arm = ARMS[ak]
        if arm.is_control:
            print(
                f"\n== {ak} :: control arm (no MCP server) — skipped in probe"
            )
            continue
        spec = arm.serve(project)
        print(f"\n== {ak} :: {spec.command} {' '.join(spec.args)}")
        transport = StdioTransport(
            command=spec.command,
            args=spec.args,
            cwd=spec.cwd,
            env=dict(spec.env) if spec.env else None,
        )
        try:
            async with Client(transport) as client:
                tools = await client.list_tools()
                print(
                    f"   connected, {len(tools)} tools: {', '.join(t.name for t in tools)}"
                )
        except Exception as exc:
            print(f"   FAILED: {exc!r}")
            rc = 1
    return rc


async def run_cell(
    project_key: str, arm_key: str, model_key: str, task_id: str | None
) -> int:
    project = PROJECTS[project_key]
    arm = ARMS[arm_key]
    tasks = load_tasks(project)
    if not tasks:
        print(f"no tasks for {project_key}")
        return 1
    task = (
        next((t for t in tasks if t.id == task_id), tasks[0])
        if task_id
        else tasks[0]
    )
    print(f"task {task.id}: {task.prompt}")
    async with ArmSession(arm, project, fetch_pricing()) as session:
        out = await session.run(model_key, task.prompt, seed=0)
    acc = score(out.answer, task.expected)
    print(f"\nanswer: {out.answer!r}")
    print(f"accuracy={acc}  tool_calls={out.n_tool_calls} ({out.tool_names})")
    print(
        f"tokens in/out/total={out.input_tokens}/{out.output_tokens}/{out.total_tokens}"
    )
    print(
        f"cost_exact={out.cost_exact} cost_table=${out.cost_table:.6f}  wall={out.wall_s:.1f}s"
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default="gin", choices=list(PROJECTS))
    ap.add_argument("--arm", default="graphlens", choices=list(ARMS))
    ap.add_argument("--model", default="qwen3-8b", choices=list(MODELS))
    ap.add_argument("--task", default=None)
    ap.add_argument(
        "--probe", action="store_true", help="list tools for all arms, no LLM"
    )
    args = ap.parse_args()
    if args.probe:
        return asyncio.run(probe(args.project, list(ARMS)))
    return asyncio.run(run_cell(args.project, args.arm, args.model, args.task))


if __name__ == "__main__":
    raise SystemExit(main())
