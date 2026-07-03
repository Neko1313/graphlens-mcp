"""
Language-aware gold proposers (ripgrep-based, ast-grep where available).

These are *cross-checks*, not ground truth: they re-derive a candidate answer
independently of the arms so a human can confirm the authored gold. They favor
recall (catch everything the author might have missed) and report candidates for
review rather than silently overwriting.
"""

from __future__ import annotations

import pathlib
import re

from bench.oracle.common import iter_source, rg_files

# Definition-line patterns per language, split into "type" (struct/class/trait/
# interface/type-alias) and "func" (function/method) so a where_defined for a
# struct isn't polluted by a same-named method elsewhere.
_TYPE_PATTERNS: dict[str, list[str]] = {
    "go": [r"^\s*type\s+{sym}\b"],
    "rust": [r"^\s*(pub\s+)?(struct|enum|trait|type|union)\s+{sym}\b"],
    "python": [r"^\s*class\s+{sym}\b"],
    "typescript": [
        r"\b(export\s+)?(default\s+)?(abstract\s+)?(class|interface|type|enum)\s+{sym}\b"
    ],
}
_FUNC_PATTERNS: dict[str, list[str]] = {
    "go": [r"^\s*func\s+{sym}\b", r"^\s*func\s+\([^)]*\)\s+{sym}\b"],
    "rust": [r"^\s*(pub\s+)?(async\s+)?fn\s+{sym}\b"],
    "python": [r"^\s*(async\s+)?def\s+{sym}\b"],
    "typescript": [
        r"\b(export\s+)?(async\s+)?function\s+{sym}\b",
        r"\b(export\s+)?const\s+{sym}\b",
    ],
}

# Call-site pattern per language: `symbol(` with a word boundary.
_CALL_PATTERN = r"\b{sym}\s*\("

_TEST_SUFFIXES = {
    "go": ("_test.go",),
    "rust": (),  # rust tests are inline; handled separately if needed
    "python": ("_test.py",),
    "typescript": (".test.ts", ".spec.ts", ".test.tsx", ".spec.tsx"),
}


def _esc(symbol: str) -> str:
    return re.escape(symbol)


def _strip_comment_lines(text: str) -> str:
    """
    Drop whole-line comments before call-site counting.

    Doc-example mentions (Rust `/// foo()`, Python `# foo()`) must not count as
    real calls. Conservative: only removes lines whose first non-space chars are
    a comment marker.
    """
    out = []
    for line in text.splitlines():
        s = line.lstrip()
        if s.startswith(("///", "//!", "//", "/*", "*", "#")):
            continue
        out.append(line)
    return "\n".join(out)


def _strip_tests(files: list[str], lang: str) -> list[str]:
    suf = _TEST_SUFFIXES.get(lang, ())
    out: list[str] = []
    for f in files:
        low = f.lower()
        if (
            "/tests/" in low
            or low.startswith("tests/")
            or "/__tests__/" in low
            or "/test/" in low
        ):
            continue
        if suf and f.endswith(suf):
            continue
        if lang == "python" and pathlib.Path(f).name.startswith("test_"):
            continue
        out.append(f)
    return out


def definition_files(
    symbol: str, lang: str, root: pathlib.Path, *, def_kind: str | None = None
) -> list[str]:
    """Files that *define* `symbol`. def_kind restricts to 'type' or 'func' defs."""
    pats: list[str] = []
    if def_kind in (None, "type"):
        pats += _TYPE_PATTERNS.get(lang, [])
    if def_kind in (None, "func"):
        pats += _FUNC_PATTERNS.get(lang, [])
    found: set[str] = set()
    for pat in pats:
        found.update(rg_files(pat.format(sym=_esc(symbol)), root, [lang]))
    return _strip_tests(sorted(found), lang)


def callers_of(
    symbol: str, lang: str, root: pathlib.Path, *, include_tests: bool = False
) -> list[str]:
    """
    Files that *call* `symbol` — i.e. the impact set.

    A file counts when it has more `symbol(` occurrences than `symbol`
    definitions (so the defining file is kept only if it ALSO calls the symbol,
    and a definition-only file is dropped). Test files are excluded by default.
    """
    call_re = re.compile(_CALL_PATTERN.format(sym=_esc(symbol)))
    def_pats = _TYPE_PATTERNS.get(lang, []) + _FUNC_PATTERNS.get(lang, [])
    def_res = [
        re.compile(p.format(sym=_esc(symbol)), re.MULTILINE) for p in def_pats
    ]
    out: list[str] = []
    for rel, text in iter_source(root, [lang]):
        code = _strip_comment_lines(
            text
        )  # ignore doc/comment mentions (esp. Rust ///)
        n_calls = len(call_re.findall(code))
        n_defs = sum(len(r.findall(code)) for r in def_res)
        if n_calls > n_defs:
            out.append(rel)
    out = sorted(out)
    return out if include_tests else _strip_tests(out, lang)


def subclasses_overriding(
    method: str, lang: str, root: pathlib.Path, *, scope: str = ""
) -> list[str]:
    """
    Files (optionally under `scope` subpath) that define a method named `method`.

    Heuristic for overrides_count: a real override is a (re)definition of the
    method in a file other than the base. The author confirms which are genuine
    subclass overrides vs unrelated same-named methods.
    """
    pats = {
        "python": r"^\s*(async\s+)?def\s+{m}\b",
        "go": r"^\s*func\s+\([^)]*\)\s+{m}\b",
        "rust": r"^\s*(pub\s+)?fn\s+{m}\b",
        "typescript": r"^\s*(public|private|protected|override|\s)*\b{m}\s*\(",
    }
    pat = pats.get(lang, r"\b{m}\b").format(m=_esc(method))
    files = rg_files(pat, root, [lang])
    if scope:
        files = [
            f
            for f in files
            if f.startswith(scope.rstrip("/") + "/") or f.startswith(scope)
        ]
    return sorted(files)


def propose(task: dict, root: pathlib.Path) -> list[str] | None:
    """
    Dispatch on task['oracle'] -> a proposed candidate set, or None if not verifiable.

    task['oracle'] examples:
      {"op": "definition_files", "symbol": "Engine", "lang": "go"}
      {"op": "callers", "symbol": "get_example_database", "lang": "python"}
      {"op": "overrides", "method": "df_to_sql", "lang": "python", "scope": "superset/db_engine_specs"}
    """
    spec = task.get("oracle")
    if not spec:
        return None
    op = spec.get("op")
    lang = spec.get("lang", "")
    if op == "definition_files":
        return definition_files(
            spec["symbol"], lang, root, def_kind=spec.get("def_kind")
        )
    if op == "callers":
        return callers_of(
            spec["symbol"],
            lang,
            root,
            include_tests=spec.get("include_tests", False),
        )
    if op == "overrides":
        return subclasses_overriding(
            spec["method"], lang, root, scope=spec.get("scope", "")
        )
    return None
