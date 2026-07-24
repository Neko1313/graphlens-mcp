"""
Per-run tool-call traces — the diagnostic view report.py can't give.

report.py answers "which arm wins". This answers "*why* did that run cost 60k
tokens": every call the agent made, with the size of what came back, so a
wasteful loop (search returning links the agent must re-resolve, info drilling
that re-reads whole files) is visible as a shape rather than a number.

    uv run scripts/traces.py --project gin --arm graphlens          # summary
    uv run scripts/traces.py --project gin --task gin_impact_next   # full trace
    uv run scripts/traces.py --sort tokens --limit 10               # worst first
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from collections import Counter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from bench.config import DATA_DIR


def load(project: str | None, arm: str | None, model: str | None) -> list[dict]:
    rows: dict[tuple, dict] = {}
    for fp in sorted(DATA_DIR.glob("*__*__*.jsonl")):
        p, a, m = fp.stem.split("__")
        if (project and p != project) or (arm and a != arm) or (model and m != model):
            continue
        for line in fp.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                rows[(r["project"], r["arm"], r["model"], r["task_id"], r["seed"])] = r
    return list(rows.values())


def _arg_digest(args: object) -> str:
    """The one or two argument values that identify what a call asked for."""
    if not isinstance(args, dict):
        return str(args)[:60]
    # Tool params arrive either flat or nested under a `params` model.
    flat = args.get("params") if isinstance(args.get("params"), dict) else args
    keys = ("query", "target", "symbol", "id", "path", "directory")
    parts = [f"{k}={flat[k]!r}" for k in keys if flat.get(k)]
    extra = [
        f"{k}={flat[k]!r}"
        for k in ("verbosity", "depth", "exhaustive", "path_glob", "mode", "limit")
        if flat.get(k)
    ]
    return " ".join(parts + extra)[:130]


def show_trace(row: dict) -> None:
    print(f"\n=== {row['task_id']} [{row['arm']}/{row['model']}] seed={row['seed']}")
    print(f"    regime={row['regime']} acc={row['accuracy']} tokens={row['total_tokens']} wall={row['wall_s']}s")
    print(f"    prompt: {row['prompt'][:150]}")
    for i, call in enumerate(row.get("tool_calls") or [], 1):
        print(f"    {i:2}. {call['tool']:<10} {_arg_digest(call.get('args'))}")
        print(f"        -> {call.get('ret_chars', 0)} chars")
    print(f"    answer: {str(row['answer'])[:200]!r}")
    print(f"    expected: {json.dumps(row['expected'])[:200]}")


def summary(rows: list[dict]) -> None:
    rows = sorted(rows, key=lambda r: (r["project"], r["task_id"], r["arm"]))
    print(
        f"{'task':<34}{'arm':<11}{'acc':>5}{'calls':>7}{'tokens':>9}{'ret_k':>7}  tools"
    )
    for r in rows:
        calls = r.get("tool_calls") or []
        ret_k = sum(c.get("ret_chars", 0) for c in calls) / 1000
        seq = Counter(c["tool"] for c in calls)
        shape = ",".join(f"{k}x{v}" for k, v in seq.items())
        print(
            f"{r['task_id']:<34}{r['arm']:<11}{r['accuracy']:>5}{r['n_tool_calls']:>7}"
            f"{r['total_tokens']:>9}{ret_k:>7.1f}  {shape}"
        )


def main() -> int:
    ap = argparse.ArgumentParser(description="per-run tool-call traces")
    ap.add_argument("--project")
    ap.add_argument("--arm")
    ap.add_argument("--model")
    ap.add_argument("--task", help="show the full trace for this task id")
    ap.add_argument("--sort", choices=("tokens", "calls", "acc"), default=None)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    rows = load(args.project, args.arm, args.model)
    if not rows:
        print("no matching rows in data/", file=sys.stderr)
        return 1
    if args.task:
        for row in rows:
            if row["task_id"] == args.task:
                show_trace(row)
        return 0
    if args.sort:
        key = {
            "tokens": lambda r: -r["total_tokens"],
            "calls": lambda r: -r["n_tool_calls"],
            "acc": lambda r: r["accuracy"],
        }[args.sort]
        rows = sorted(rows, key=key)
    if args.limit:
        rows = rows[: args.limit]
    summary(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
