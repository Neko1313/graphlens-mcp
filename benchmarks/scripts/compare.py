"""
Before/after diff of two result sets — did a fix actually help?

The iteration loop (measure -> fix -> re-measure) needs the paired view that
report.py doesn't give: the same task, same arm, same model, two runs, and the
delta in accuracy / tool calls / tokens.

    uv run scripts/compare.py data/archive/iter0-baseline data
    uv run scripts/compare.py <before> <after> --arm graphlens
"""

from __future__ import annotations

import argparse
import json
import pathlib
import statistics
import sys


def load(root: pathlib.Path, arm: str | None) -> dict[tuple, dict]:
    rows: dict[tuple, dict] = {}
    for fp in sorted(root.glob("*__*__*.jsonl")):
        for line in fp.read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if arm and r["arm"] != arm:
                continue
            rows[(r["project"], r["arm"], r["model"], r["task_id"], r["seed"])] = r
    return rows


def _fmt(before: float, after: float, *, lower_is_better: bool) -> str:
    delta = after - before
    if not delta:
        return "  ="
    good = delta < 0 if lower_is_better else delta > 0
    return f"{'+' if delta > 0 else ''}{delta:.0f} {'✓' if good else '✗'}"


def main() -> int:
    ap = argparse.ArgumentParser(description="paired before/after comparison")
    ap.add_argument("before", type=pathlib.Path)
    ap.add_argument("after", type=pathlib.Path)
    ap.add_argument("--arm")
    args = ap.parse_args()

    before, after = load(args.before, args.arm), load(args.after, args.arm)
    shared = sorted(before.keys() & after.keys())
    if not shared:
        print("no overlapping (project, arm, model, task, seed) cells", file=sys.stderr)
        return 1

    print(f"{'task':<34}{'acc':>14}{'calls':>16}{'tokens':>20}")
    for key in shared:
        b, a = before[key], after[key]
        print(
            f"{b['task_id']:<34}"
            f"{b['accuracy']:>6.2f}->{a['accuracy']:<8.2f}"
            f"{b['n_tool_calls']:>6}->{a['n_tool_calls']:<4}{_fmt(b['n_tool_calls'], a['n_tool_calls'], lower_is_better=True):<6}"
            f"{b['total_tokens']:>8}->{a['total_tokens']:<8}{_fmt(b['total_tokens'], a['total_tokens'], lower_is_better=True)}"
        )

    def agg(rows: dict[tuple, dict], field: str) -> tuple[float, float]:
        vals = [rows[k][field] for k in shared]
        return statistics.mean(vals), statistics.median(vals)

    print()
    for field, lower in (("accuracy", False), ("n_tool_calls", True), ("total_tokens", True)):
        bm, bmed = agg(before, field)
        am, amed = agg(after, field)
        arrow = "better" if ((am < bm) == lower and am != bm) else ("same" if am == bm else "worse")
        print(f"{field:<14} mean {bm:>10.2f} -> {am:<10.2f}  median {bmed:>10.1f} -> {amed:<10.1f}  {arrow}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
