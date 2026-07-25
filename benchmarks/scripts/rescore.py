"""
Re-score recorded runs against the current gold.

Accuracy is computed at run time and written into each row, so correcting a
gold answer silently leaves every past row scored against the old one — and
the before/after comparison that the correction was meant to inform is the
first thing it corrupts. Scoring is deterministic and the answers are stored,
so re-scoring is exact; nothing is re-run and no tokens are spent.

    uv run scripts/rescore.py data data/archive/iter0-baseline    # in place
    uv run scripts/rescore.py --dry-run data
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from bench.dataset import load_tasks
from bench.projects import PROJECTS
from bench.scoring import score


def gold_by_task() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for project in PROJECTS.values():
        for task in load_tasks(project):
            out[task.id] = task.expected
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="re-score rows against current gold")
    ap.add_argument("dirs", nargs="+", type=pathlib.Path)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    gold = gold_by_task()
    changed = 0
    for root in args.dirs:
        for fp in sorted(root.glob("*__*__*.jsonl")):
            lines, dirty = [], False
            for line in fp.read_text().splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                expected = gold.get(row["task_id"]) or row.get("expected")
                # Recomputed unconditionally: the scorer itself changes too, not
                # only the gold, and a row scored under the old rule is exactly
                # as stale as one scored against the old answer.
                fresh = round(score(str(row["answer"]), expected), 4)
                if expected != row.get("expected") or fresh != row["accuracy"]:
                    print(
                        f"{fp.name} {row['task_id']} seed={row['seed']}: "
                        f"{row['accuracy']} -> {fresh}"
                    )
                    row["expected"], row["accuracy"] = expected, fresh
                    dirty, changed = True, changed + 1
                lines.append(json.dumps(row, ensure_ascii=False))
            if dirty and not args.dry_run:
                fp.write_text("\n".join(lines) + "\n")
    print(f"{changed} row(s) re-scored{' (dry run)' if args.dry_run else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
