"""
Deterministic grading — no LLM judge, tool-agnostic.

Two task shapes, both graded against hand/oracle-verified gold (never against a
tool's own output, which would bias that arm):

  - ``answer_contains``: list of required substrings. Score = fraction present
    (case-insensitive). Used for pinpoint lookups (one file / one class name).

  - ``answer_set``: an unordered gold set. Score = F1 of the answer's extracted
    items vs gold. Precision penalizes over-listing (a grep dump that names every
    candidate); recall penalizes misses. Used for impact / overrides tasks.

Set extraction has three modes, auto-detected from the gold's shape or forced via
``expected["match"]``:
  - ``path``  : repo-relative source paths (.py/.go/.rs/.ts/...).
  - ``camel`` : multi-word CamelCase identifiers (class/type/trait names).
  - ``line``  : one item per line — relies on the system prompt's "one per line"
                instruction; the general fallback for snake_case / mixed names.
"""

from __future__ import annotations

import re

# Error sentinels: an answer starting with "__" is a harness failure, scored 0.
ERR_PREFIX = "__"
ERR_NO_TOOLS = "__NO_TOOLS__"
ERR_RUN_ERROR = "__RUN_ERROR__"
ERR_MAX_TURNS = "__MAX_TURNS__"
ERR_RATE_LIMITED = "__RATE_LIMITED__"
ERR_TIMEOUT = "__TIMEOUT__"

_SRC_EXT = r"(?:py|go|rs|ts|tsx|js|jsx|mjs|cjs|pyi)"
_PATH_RE = re.compile(rf"[\w./\-]+\.{_SRC_EXT}\b")
# Multi-word CamelCase: BigQueryEngineSpec, RouterGroup, ServeHTTP-ish.
_CAMEL_RE = re.compile(
    r"\b[A-Z][A-Za-z0-9]*[a-z][A-Za-z0-9]*[A-Z][A-Za-z0-9]*\b"
)
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s*")
# In line mode, ignore lines longer than this — real answer items are short
# identifiers/paths, not prose sentences.
_MAX_ITEM_LEN = 120


def _looks_like_path(item: str) -> bool:
    return "/" in item or bool(re.search(rf"\.{_SRC_EXT}$", item))


def _looks_camel(item: str) -> bool:
    return bool(_CAMEL_RE.fullmatch(item))


def _detect_mode(gold: list[str]) -> str:
    if any(_looks_like_path(g) for g in gold):
        return "path"
    if all(_looks_camel(g) for g in gold):
        return "camel"
    return "line"


def _normalize_path(s: str) -> str:
    return s.strip().strip("`'\"").lstrip("./").lower()


def _extract(answer: str, mode: str) -> set[str]:
    """Candidate items the answer *claims* — used for the precision denominator."""
    if mode == "path":
        return {_normalize_path(m) for m in _PATH_RE.findall(answer)}
    if mode == "camel":
        return {m.lower() for m in _CAMEL_RE.findall(answer)}
    # line mode: strip bullets/backticks, keep non-empty short-ish tokens.
    out: set[str] = set()
    for raw in answer.splitlines():
        line = _BULLET_RE.sub("", raw).strip().strip("`'\".,;:()").strip()
        if line and len(line) <= _MAX_ITEM_LEN and " " not in line.strip():
            out.add(line.lower())
    return out


def _f1(found: int, listed: int, gold: int) -> float:
    fp = max(0, listed - found)
    fn = max(0, gold - found)
    prec = found / (found + fp) if (found + fp) else 1.0
    rec = found / (found + fn) if (found + fn) else 0.0
    return 0.0 if (prec + rec) == 0 else 2 * prec * rec / (prec + rec)


def score(answer: str, expected: dict) -> float:
    """0.0–1.0 correctness for one answer against its gold spec."""
    if not answer or answer.startswith(ERR_PREFIX):
        return 0.0

    gold_set = expected.get("answer_set")
    if gold_set:
        mode = expected.get("match") or _detect_mode(gold_set)
        norm = _normalize_path if mode == "path" else str.lower
        gset = {norm(g) for g in gold_set}
        listed = _extract(answer, mode)
        found = len(gset & listed)
        # Recall is also credited for substring presence (model may phrase loosely),
        # but precision is judged on the extracted `listed` set.
        a_low = answer.lower()
        substr_found = {g for g in gset if g in a_low}
        found = max(found, len(substr_found))
        return _f1(found, max(len(listed), found), len(gset))

    wanted = [w.lower() for w in expected.get("answer_contains", [])]
    if not wanted:
        return 0.0
    a_low = answer.lower()
    return sum(w in a_low for w in wanted) / len(wanted)


def is_error(answer: str) -> bool:
    return not answer or answer.startswith(ERR_PREFIX)
