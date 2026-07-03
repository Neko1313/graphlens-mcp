"""
File-search primitives for the oracle.

Self-contained: a pure-Python regex walker is the reliable backend (these target
repos are small), with a fast ripgrep path used only when a *real* `rg` binary is
found (the Claude Code shell ships `rg` as a shell function, not a PATH binary, so
shutil.which is not enough — we probe known locations too). ast-grep is optional.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

# File globs per language.
LANG_EXTS: dict[str, tuple[str, ...]] = {
    "go": (".go",),
    "rust": (".rs",),
    "python": (".py", ".pyi"),
    "typescript": (".ts", ".tsx"),
}

SG_LANG: dict[str, str] = {
    "go": "go",
    "rust": "rust",
    "python": "python",
    "typescript": "typescript",
}

# Directories never worth scanning.
_SKIP_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
    ".graphlens",
    ".codegraph",
    "vendor",
    "target",
    "__pycache__",
}


def have(tool: str) -> bool:
    return shutil.which(tool) is not None


def _real_rg() -> str | None:
    """Return a genuine ripgrep *executable* (not the Claude shell function)."""
    p = shutil.which("rg")
    if p and os.access(p, os.X_OK) and not p.endswith(".sh"):
        return p
    for cand in (
        "/usr/lib/node_modules/@anthropic-ai/claude-code/vendor/ripgrep/x64-linux/rg",
        "/usr/lib/node_modules/@anthropic-ai/claude-code/vendor/ripgrep/arm64-linux/rg",
        str(pathlib.Path.home() / ".cargo/bin/rg"),
    ):
        if os.path.exists(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def _iter_files(
    root: pathlib.Path, langs: list[str] | None
) -> Iterator[pathlib.Path]:
    exts: tuple[str, ...] | None = None
    if langs:
        exts = tuple(e for lang in langs for e in LANG_EXTS.get(lang, ()))
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            if exts and not fn.endswith(exts):
                continue
            yield pathlib.Path(dirpath) / fn


def rg_files(
    pattern: str,
    root: pathlib.Path,
    langs: list[str] | None = None,
    *,
    fixed: bool = False,
) -> list[str]:
    """Repo-relative paths of files with at least one line matching `pattern`."""
    rg = _real_rg()
    if rg:
        cmd = [rg, "-l", "--no-messages"]
        if fixed:
            cmd.append("-F")
        cmd.append(pattern)
        for lang in langs or []:
            for e in LANG_EXTS.get(lang, ()):
                cmd += ["-g", f"*{e}"]
        cmd.append(".")
        proc = subprocess.run(
            cmd, cwd=root, capture_output=True, text=True, check=False
        )
        return sorted(p for p in proc.stdout.splitlines() if p)

    # Pure-Python fallback.
    rx = re.compile(re.escape(pattern) if fixed else pattern)
    out: set[str] = set()
    for path in _iter_files(root, langs):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if rx.search(text):
            out.add(str(path.relative_to(root)))
    return sorted(out)


def iter_source(
    root: pathlib.Path, langs: list[str] | None
) -> Iterator[tuple[str, str]]:
    """Yield (relpath, text) per source file (lang-filtered, skip-dirs applied)."""
    for path in _iter_files(root, langs):
        try:
            yield (
                str(path.relative_to(root)),
                path.read_text(encoding="utf-8", errors="ignore"),
            )
        except OSError:
            continue


def sg_run(pattern: str, lang: str, root: pathlib.Path) -> list[dict]:
    """ast-grep matches (optional; empty list if ast-grep is unavailable)."""
    if not have("ast-grep"):
        return []
    cmd = [
        "ast-grep",
        "run",
        "-p",
        pattern,
        "-l",
        SG_LANG.get(lang, lang),
        "--json=stream",
        ".",
    ]
    proc = subprocess.run(
        cmd, cwd=root, capture_output=True, text=True, check=False
    )
    out: list[dict] = []
    for line in proc.stdout.splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out
