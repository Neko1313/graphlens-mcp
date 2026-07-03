"""Aggregate data/*.jsonl into stratified, contamination-aware result tables.

    uv run scripts/report.py                 # all results
    uv run scripts/report.py --project gin   # one project

Design choices that follow current benchmark methodology:
  - **Regime split**: SIMPLE lookups vs HARD impact/disambiguation reported
    separately, never pooled (pooling hides the regime-dependent conclusion).
  - **Cost-controlled**: accuracy is always shown next to tokens / tool-calls /
    cost — a cheaper arm at equal accuracy wins.
  - **Contamination-aware**: a `none` control arm (no tools) gives the pure
    model-memory baseline; each real arm's `lift` over it is the tool's true
    contribution. `ungrounded` = share of answers given with zero tool calls
    (high => the model is answering from memory, not the tool).
  - **Reliability**: efficiency stats are medians over completed runs; accuracy
    carries a per-task seed spread (`acc_sd`).
  - **Significance**: a Friedman test ranks arms within a regime (non-parametric,
    blocked by task) — see the printed critical values.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pandas as pd

from bench.config import DATA_DIR

# Friedman chi-square critical values (alpha=0.05 / 0.01) by degrees of freedom
# (k-1, where k = #arms compared). Lets us flag significance without scipy.
_CHI2_CRIT = {
    2: (5.99, 9.21),
    3: (7.82, 11.34),
    4: (9.49, 13.28),
    5: (11.07, 15.09),
}


def load() -> pd.DataFrame:
    # Keyed by the last occurrence of (project, arm, model, task_id, seed):
    # a retried cell is appended after its original row rather than replacing
    # it in place, so a naive concat double-counts every retried failure.
    rows: dict[tuple, dict] = {}
    for fp in sorted(DATA_DIR.glob("*__*__*.jsonl")):
        for line in fp.read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            key = (r["project"], r["arm"], r["model"], r["task_id"], r["seed"])
            rows[key] = r
    if not rows:
        sys.exit("no result rows in data/ — run `uv run main.py` first")
    df = pd.DataFrame(rows.values())
    df["is_error"] = df["answer"].astype(str).str.startswith("__")
    df["completed"] = ~df["is_error"]
    # "ungrounded" = a *successful* answer given with zero tool calls (answered
    # from memory). Errored runs (which also report 0 calls) are excluded.
    df["ungrounded"] = (
        (df["n_tool_calls"] == 0) & df["completed"] & (df["arm"] != "none")
    )
    df["cost"] = df["cost_exact"].where(
        df["cost_exact"].notna(), df["cost_table"]
    )
    return df


def table(df: pd.DataFrame, by: list[str]) -> pd.DataFrame:
    g = df.groupby(by)
    comp = df[df["completed"]].groupby(by)
    # Per-task seed spread, then averaged -> a reliability signal.
    sd = df.groupby([*by, "task_id"])["accuracy"].std().groupby(by).mean()
    return pd.DataFrame(
        {
            "n": g.size(),
            "accuracy": g["accuracy"].mean().round(3),
            "acc_sd": sd.round(3),
            "completion": g["completed"].mean().round(3),
            "ungrounded": g["ungrounded"].mean().round(3),
            "med_tokens": comp["total_tokens"].median().round(0),
            "med_calls": comp["n_tool_calls"].median().round(1),
            "med_cost": comp["cost"].median().round(5),
            "med_wall_s": comp["wall_s"].median().round(1),
        }
    )


def lift_over_control(df: pd.DataFrame) -> pd.DataFrame | None:
    """Each arm's accuracy minus the `none` control arm's, per (regime)."""
    if "none" not in df["arm"].unique():
        return None
    base = df[df["arm"] == "none"].groupby("regime")["accuracy"].mean()
    arm = df.groupby(["regime", "arm"])["accuracy"].mean()
    lift = (arm - base).round(3)
    return lift.unstack("arm")


def friedman(df: pd.DataFrame) -> tuple[float, int] | None:
    """Friedman chi-square over arms, blocked by task_id (mean acc per task×arm).

    Excludes the control arm — the question is which *tool* ranks best, not
    whether tools beat memory (that's `lift`).
    """
    sub = df[df["arm"] != "none"]
    pivot = sub.groupby(["task_id", "arm"])["accuracy"].mean().unstack("arm")
    pivot = pivot.dropna(axis=0, how="any")  # tasks every arm attempted
    n, k = pivot.shape
    if n < 2 or k < 2:
        return None
    ranks = pivot.rank(
        axis=1
    )  # rank arms within each task (higher acc = higher rank)
    rj = ranks.sum(axis=0)
    chi2 = 12.0 / (n * k * (k + 1)) * float((rj**2).sum()) - 3 * n * (k + 1)
    return round(chi2, 2), k - 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default=None)
    args = ap.parse_args()
    df = load()
    if args.project:
        df = df[df["project"] == args.project]

    pd.set_option("display.width", 160)
    pd.set_option("display.max_columns", 24)

    lift = lift_over_control(df)
    if lift is not None:
        print(
            "\n###### accuracy LIFT over `none` control (tool's true contribution) ######"
        )
        print(lift.to_string())

    for regime in ("SIMPLE", "HARD"):
        sub = df[df["regime"] == regime]
        if sub.empty:
            continue
        print(
            f"\n############## {regime} ({sub['task_id'].nunique()} tasks) ##############"
        )
        print("\n-- by arm --")
        print(table(sub, ["arm"]).to_string())
        print("\n-- by arm × model --")
        print(table(sub, ["arm", "model"]).to_string())
        if sub["project"].nunique() > 1:
            print("\n-- by arm × project --")
            print(table(sub, ["arm", "project"]).to_string())
        fr = friedman(sub)
        if fr:
            chi2, dfree = fr
            crit = _CHI2_CRIT.get(dfree)
            verdict = ""
            if crit:
                verdict = (
                    " p<0.01"
                    if chi2 > crit[1]
                    else " p<0.05"
                    if chi2 > crit[0]
                    else " n.s."
                )
                verdict += f" (crit .05/.01 = {crit[0]}/{crit[1]})"
            print(f"\nFriedman χ²={chi2}  df={dfree}{verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
