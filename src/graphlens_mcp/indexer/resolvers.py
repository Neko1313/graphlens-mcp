"""Resolver lifecycle management and toolchain doctor."""

from __future__ import annotations

import importlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from graphlens import (
    LanguageAdapter,
    ResolverStatus,
    adapter_registry,
)

if TYPE_CHECKING:
    from graphlens.contracts.resolver import SymbolResolver

logger = logging.getLogger(__name__)

# Each language can live in several independent packages within one repo
# (a uv / pnpm / cargo workspace). The engine's full index discovers these
# per-package roots and keys every node id off the *package* name and the
# package-relative module path. Incremental re-indexing has to use the same
# roots or it re-keys a member's symbols under the wrong project and breaks
# every cross-file edge into them. These are the engine's own per-language
# root finders; we import them lazily and defensively so a missing adapter
# (or an upstream rename) degrades to single-root behaviour, never a crash.
_ROOT_FINDERS: dict[str, tuple[str, str]] = {
    "python": ("graphlens_python._project_detector", "find_python_roots"),
    "typescript": (
        "graphlens_typescript._project_detector",
        "find_typescript_roots",
    ),
    "go": ("graphlens_go._project_detector", "find_go_roots"),
    "rust": ("graphlens_rust._project_detector", "find_rust_roots"),
    "php": ("graphlens_php._project_detector", "find_php_roots"),
}


def find_language_roots(language: str, project_root: Path) -> list[Path]:
    """
    Return the per-package project roots for *language* under *project_root*.

    In a monorepo one language can span several independent packages, each
    its own root with its own project name. The full index analyzes each
    such root separately, so incremental re-indexing must group changed
    files by the same roots to reproduce identical node ids. Falls back to
    ``[project_root]`` when no finder is available — exactly the
    single-root behaviour, which is correct for a plain (non-workspace)
    repository.
    """
    spec = _ROOT_FINDERS.get(language)
    if spec is None:
        return [project_root]
    module_name, func_name = spec
    try:
        module = importlib.import_module(module_name)
        finder = getattr(module, func_name)
        roots = [Path(r).resolve() for r in finder(project_root)]
    except Exception as exc:
        logger.debug("Root discovery for %s failed: %s", language, exc)
        return [project_root]
    return roots or [project_root]


INSTALL_HINTS: dict[str, str] = {
    "python": (
        "ty is bundled as a dependency — run `pip install graphlens-python`"
    ),
    "go": "Install Go toolchain: https://go.dev/dl/",
    "rust": "Install Rust toolchain: https://rustup.rs/",
    "typescript": "Install Node.js: https://nodejs.org/",
    "php": "Install PHP: https://www.php.net/downloads",
}


def _adapter_cls(language: str) -> type[LanguageAdapter] | None:
    try:
        return adapter_registry.load(language)
    except Exception:
        return None


def get_adapter(language: str) -> LanguageAdapter | None:
    """Return a configured adapter for *language*, or None if missing."""
    cls = _adapter_cls(language)
    if cls is None:
        return None
    try:
        return cls()
    except Exception:
        logger.warning("Failed to instantiate adapter for %s", language)
        return None


def probe_resolver_status(language: str, project_root: Path) -> ResolverStatus:
    """Run prepare() on the adapter and return its resolver status."""
    adapter = get_adapter(language)
    if adapter is None:
        return ResolverStatus.UNAVAILABLE

    try:
        resolver = _get_resolver(adapter)
        if resolver is None:
            return ResolverStatus.UNAVAILABLE
        resolver.prepare(project_root, [])
        return resolver.status()
    except Exception:
        return ResolverStatus.UNAVAILABLE


def _get_resolver(adapter: LanguageAdapter) -> SymbolResolver | None:
    return getattr(adapter, "_resolver", None)


def doctor(project_root: Path) -> dict[str, dict[str, Any]]:
    """
    Check each available language adapter and return a status report.

    Returns::

        {
            "python": {"status": "ok", "hint": None},
            "go": {
                "status": "unavailable",
                "hint": "Install Go toolchain …",
            },
        }
    """
    report: dict[str, dict[str, Any]] = {}

    for lang in adapter_registry.available():
        adapter = get_adapter(lang)
        if adapter is None:
            status = ResolverStatus.UNAVAILABLE
        elif not adapter.can_handle(project_root):
            continue
        else:
            # Reuse the already-instantiated adapter instead of re-creating one
            resolver = _get_resolver(adapter)
            if resolver is None:
                status = ResolverStatus.UNAVAILABLE
            else:
                try:
                    resolver.prepare(project_root, [])
                    status = resolver.status()
                except Exception:
                    status = ResolverStatus.UNAVAILABLE

        hint: str | None = None
        if status != ResolverStatus.OK:
            hint = INSTALL_HINTS.get(
                lang, f"Check graphlens-{lang} adapter docs"
            )

        report[lang] = {"status": status.value, "hint": hint}

    return report
