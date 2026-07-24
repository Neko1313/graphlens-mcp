"""
Emit the README / docs headline table straight from data/ — no hand-typing.

The published tables (README's BENCHMARK-RESULTS block, docs/benchmarks.md) show,
per arm, the RANGE of a metric across the models: min–max accuracy, token
spend, completion. Regenerating them by hand after a re-run is how stale numbers
creep in, so this prints the exact markdown from the recorded rows.

    uv run scripts/readme_table.py            # markdown table + one-line ranges
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pandas as pd

from bench.config import DATA_DIR

ARM_ORDER = ["graphlens", "codegraph", "semble", "none"]


def load() -> pd.DataFrame:
    rows: dict[tuple, dict] = {}
    for fp in sorted(DATA_DIR.glob("*__*__*.jsonl")):
        for line in fp.read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            key = (r["project"], r["arm"], r["model"], r["task_id"], r["seed"])
            rows[key] = r
    df = pd.DataFrame(rows.values())
    df["is_error"] = df["answer"].astype(str).str.startswith("__")
    df["completed"] = ~df["is_error"]
    return df


def _rng(series: pd.Series, scale: float = 1.0, unit: str = "") -> str:
    lo, hi = series.min() / scale, series.max() / scale
    if unit == "k":
        return f"{lo:.1f}k – {hi:.1f}k"
    return f"{lo:.3f} – {hi:.3f}"


def per_arm_model(df: pd.DataFrame, regime: str) -> pd.DataFrame:
    sub = df[df["regime"] == regime]
    ok = sub[sub["completed"]]
    acc = ok.groupby(["arm", "model"])["accuracy"].mean()
    tok = ok.groupby(["arm", "model"])["total_tokens"].median()
    comp = sub.groupby(["arm", "model"])["completed"].mean()
    return pd.DataFrame({"acc": acc, "tok": tok, "comp": comp}).reset_index()


def main() -> int:
    df = load()
    models = sorted(df["model"].unique())
    n_runs = len(df)
    print(f"# {df['project'].nunique()} repos · {len(models)} models "
          f"({', '.join(models)}) · {n_runs} graded runs\n")

    simple = per_arm_model(df, "SIMPLE")
    hard = per_arm_model(df, "HARD")

    print("| | SIMPLE accuracy | HARD accuracy | HARD tokens (median) "
          "| HARD completion |")
    print("|---|---|---|---|---|")
    arms = [a for a in ARM_ORDER if a in df["arm"].unique()]
    for arm in arms:
        s = simple[simple["arm"] == arm]
        h = hard[hard["arm"] == arm]
        label = f"**{arm}**" if arm == "graphlens" else arm
        if arm == "none":
            label = "none (control)"
        s_acc = _rng(s["acc"]) if len(s) else "—"
        h_acc = _rng(h["acc"]) if len(h) else "—"
        h_tok = _rng(h["tok"], scale=1000, unit="k") if len(h) else "—"
        h_comp = (
            f"{h['comp'].min():.3f} – {h['comp'].max():.3f}"
            if len(h)
            else "—"
        )
        print(f"| {label} | {s_acc} | {h_acc} | {h_tok} | {h_comp} |")

    # lift over control, per regime — the contamination-robust headline
    print("\n## lift over none-control (mean accuracy)")
    for regime in ("SIMPLE", "HARD"):
        pam = per_arm_model(df, regime)
        base = pam[pam["arm"] == "none"]["acc"].mean()
        gl = pam[pam["arm"] == "graphlens"]["acc"].mean()
        print(f"  {regime}: graphlens {gl:.3f} vs control {base:.3f} "
              f"= +{gl - base:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
