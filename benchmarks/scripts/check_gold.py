"""Cross-check authored gold against the independent oracle.

    uv run scripts/check_gold.py                 # all projects
    uv run scripts/check_gold.py --projects gin  # one project

For every task that carries an `oracle` spec, re-derive a candidate answer from
ripgrep/ast-grep and compare it to the task's gold. AGREE => high confidence;
REVIEW => a human should look (the oracle is recall-biased, so REVIEW often means
the oracle over-proposes, not that the gold is wrong — but it flags what to check).
Tasks without an `oracle` spec are listed as MANUAL.
"""

from __future__ import annotations

import argparse
import json
import sys

sys.path.insert(
    0, str(__import__("pathlib").Path(__file__).resolve().parent.parent)
)

from bench.config import TASKS_DIR
from bench.oracle import propose
from bench.projects import PROJECTS, projects_for
from bench.scoring import _detect_mode, _normalize_path


def _gold_items(expected: dict) -> tuple[set[str], str]:
    if "answer_set" in expected:
        gold = expected["answer_set"]
        mode = expected.get("match") or _detect_mode(gold)
    else:
        gold = expected.get("answer_contains", [])
        mode = "path" if any("/" in g or "." in g for g in gold) else "camel"
    norm = _normalize_path if mode == "path" else str.lower
    return {norm(g) for g in gold}, mode


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--projects", nargs="*", choices=list(PROJECTS))
    args = ap.parse_args()

    n_agree = n_review = n_manual = n_missing = 0
    for project in projects_for(args.projects):
        path = TASKS_DIR / f"{project.key}.jsonl"
        if not path.exists():
            continue
        print(f"\n== {project.key} ({path.name}) ==")
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            task = json.loads(line)
            if not project.path.exists():
                print(
                    f"  ? {task['id']}: target not cloned — run `uv run main.py --setup-only`"
                )
                n_missing += 1
                continue
            proposed = propose(task, project.analyze_path)
            if proposed is None:
                print(f"  · MANUAL {task['id']} ({task['kind']})")
                n_manual += 1
                continue
            gold, mode = _gold_items(task["expected"])
            norm = _normalize_path if mode == "path" else str.lower
            prop = {norm(p) for p in proposed}
            missing = gold - prop  # gold the oracle did NOT find -> suspicious
            extra = (
                prop - gold
            )  # oracle found beyond gold -> author may have under-listed
            if not missing and not extra:
                print(f"  ✓ AGREE  {task['id']}")
                n_agree += 1
            else:
                n_review += 1
                print(f"  ⚠ REVIEW {task['id']} ({task['kind']})")
                if missing:
                    print(
                        f"      gold not confirmed by oracle: {sorted(missing)}"
                    )
                if extra:
                    print(
                        f"      oracle also proposes (≤8): {sorted(extra)[:8]}"
                    )

    print(
        f"\nsummary: AGREE={n_agree} REVIEW={n_review} MANUAL={n_manual} "
        f"NOT-CLONED={n_missing}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
