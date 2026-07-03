"""
Load task sets from tasks/<project>.jsonl.

Task record schema (one JSON object per line):

    {
      "id":       "gin_def_engine",          # unique within its project
      "kind":     "where_defined",           # task family (see TASK_KINDS)
      "prompt":   "In which source file is the `Engine` struct defined?",
      "expected": {"answer_contains": ["gin.go"]},   # or {"answer_set": [...], "match": "path"}
      "ref":      "gin.go:56",               # source-of-truth pointer (oracle/manual)
      "verified": "v1.10.0",                 # tag the gold was verified at
      "gold_src": "oracle:go"                # how gold was produced (oracle:<lang> | manual)
    }

`kind` is metadata for stratified analysis (SIMPLE lookups vs HARD impact/disambig);
grading is driven purely by `expected` (see bench.scoring).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bench.config import TASKS_DIR

if TYPE_CHECKING:
    from bench.projects import Project

# Difficulty regimes — reported separately, never pooled (pooling hides the
# regime-dependent conclusion).
SIMPLE_KINDS = frozenset(
    {
        "where_defined",
        "inherits_from",
        "implements",
        "abstract_methods",
        "signature",
        "route_handler",
        "route_call",
    }
)
HARD_KINDS = frozenset(
    {
        "callers",
        "impact_set",
        "overrides_count",
        "implementors",
        "disambiguate",
        "xlang_link",
    }
)
TASK_KINDS = SIMPLE_KINDS | HARD_KINDS


@dataclass(frozen=True)
class Task:
    id: str
    project: str
    kind: str
    prompt: str
    expected: dict
    ref: str = ""
    verified: str = ""
    gold_src: str = ""

    @property
    def regime(self) -> str:
        return "HARD" if self.kind in HARD_KINDS else "SIMPLE"


def load_tasks(project: Project) -> list[Task]:
    path = TASKS_DIR / f"{project.key}.jsonl"
    if not path.exists():
        return []
    tasks: list[Task] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        t = json.loads(line)
        tasks.append(
            Task(
                id=t["id"],
                project=project.key,
                kind=t["kind"],
                prompt=t["prompt"],
                expected=t["expected"],
                ref=t.get("ref", ""),
                verified=t.get("verified", ""),
                gold_src=t.get("gold_src", ""),
            )
        )
    return tasks
