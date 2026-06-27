"""Resolver lifecycle management and toolchain doctor."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from graphlens import (
    LanguageAdapter,
    ResolverStatus,
    adapter_registry,
)

if TYPE_CHECKING:
    from pathlib import Path

    from graphlens.contracts.resolver import SymbolResolver

logger = logging.getLogger(__name__)


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
