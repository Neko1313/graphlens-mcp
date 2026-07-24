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


_TRAILING_FENCE_RE = re.compile(
    r"```[a-zA-Z0-9_+-]*\n(.*?)```\s*\Z",
    re.DOTALL,
)


def _final_answer(answer: str) -> str:
    """
    Return the model's own delimitation of its answer, if it gave one.

    The system prompt asks for the bare list, but models routinely reason first
    and then fence the list. Scoring the whole message makes precision measure
    the *prose*: an answer whose explanation says "core.py only mentions it in
    docstrings, so it is excluded" was scored as if it had listed core.py — the
    reasoning that got the answer right is what dragged the score down, and two
    identical conclusions graded differently because one explained itself more.

    Only a fence that *ends* the message counts. Fences are also how models
    quote source, and an answer that showed a ```ts snippet mid-explanation
    and then listed its files in plain text scored 0 when the snippet was
    mistaken for the answer.
    """
    match = _TRAILING_FENCE_RE.search(answer)
    if match:
        return match.group(1)
    tail = _trailing_list(answer)
    return tail if tail else answer


def _is_item_line(line: str) -> bool:
    """Whether a line is a bare list item: one short token, no sentence."""
    item = _BULLET_RE.sub("", line).strip().strip("`'\"")
    return bool(item) and len(item) <= _MAX_ITEM_LEN and " " not in item


def _trailing_list(answer: str) -> str:
    """
    Return the run of bare list lines the message ends with, if any.

    The other half of the same problem as the fence: asked for one item per
    line, a model explains itself and *then* lists. Everything above the list
    is reasoning — including the files it names in order to rule them out —
    and grading it as claims is what turned a correct answer into a 0.25.
    """
    lines = answer.strip().splitlines()
    tail: list[str] = []
    for line in reversed(lines):
        if not line.strip():
            if tail:
                break
            continue
        if not _is_item_line(line):
            break
        tail.append(line)
    return "\n".join(reversed(tail))


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
        final = _final_answer(answer)
        listed = _extract(final, mode)
        found = len(gset & listed)
        # Recall is also credited for substring presence (model may phrase loosely),
        # but precision is judged on the extracted `listed` set.
        a_low = final.lower()
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
