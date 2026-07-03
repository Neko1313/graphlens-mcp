"""Fill the gold `answer_set` of every oracle-backed `callers` task from the oracle.

    uv run scripts/sync_oracle_gold.py            # all projects (targets must be cloned)
    uv run scripts/sync_oracle_gold.py --projects clap

For impact_set tasks whose `oracle.op == "callers"`, the ground truth IS the
independent oracle's output (text call-site derivation, comment-stripped, blind to
the graph arms). Writing it programmatically removes hand-transcription drift and
guarantees check_gold AGREE. where_defined / class-name sets are left untouched
(those stay hand-verified). Run after editing oracle logic or re-pinning a tag.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from bench.config import TASKS_DIR
from bench.oracle.proposers import callers_of
from bench.projects import PROJECTS, projects_for


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--projects", nargs="*", choices=list(PROJECTS))
    args = ap.parse_args()

    changed = 0
    for project in projects_for(args.projects):
        path = TASKS_DIR / f"{project.key}.jsonl"
        if not path.exists() or not project.path.exists():
            continue
        lines, dirty = [], False
        for raw in path.read_text().splitlines():
            if not raw.strip() or raw.startswith("#"):
                lines.append(raw)
                continue
            t = json.loads(raw)
            o = t.get("oracle")
            if o and o.get("op") == "callers":
                gold = callers_of(
                    o["symbol"], o.get("lang", ""), project.analyze_path
                )
                if t["expected"].get("answer_set") != gold:
                    t["expected"]["answer_set"] = gold
                    t["expected"].setdefault("match", "path")
                    dirty = True
                    changed += 1
                    print(f"  {project.key}/{t['id']}: {gold}")
            lines.append(json.dumps(t, ensure_ascii=False))
        if dirty:
            path.write_text("\n".join(lines) + "\n")
    print(f"\nsynced {changed} caller golds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
