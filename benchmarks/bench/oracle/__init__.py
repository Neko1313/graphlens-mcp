"""
Independent gold-answer oracle.

The cardinal rule: gold answers must come from a source *other than the tools
under test*, or an arm gets graded against its own output and the comparison is
rigged. These proposers derive candidate answers from neutral, well-understood
tools — ripgrep (text), ast-grep (syntax), and the language toolchains
(gopls / rust-analyzer / ty-pyright / tsserver) — none of which is a benchmark arm.

Workflow (scripts/check_gold.py):
  1. author a task with a hand-believed gold answer in tasks/<project>.jsonl,
  2. the oracle independently re-derives the answer from the repo,
  3. agreement => high confidence; disagreement => flagged for manual spot-check.
"""

from __future__ import annotations

from bench.oracle.proposers import (
    callers_of,
    definition_files,
    propose,
    subclasses_overriding,
)

__all__ = [
    "callers_of",
    "definition_files",
    "propose",
    "subclasses_overriding",
]
