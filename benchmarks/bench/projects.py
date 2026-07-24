"""
Target codebases — pure config, no side effects.

Each project is a real repo pinned to an immutable tag so gold answers stay
valid. Sizes are chosen to index in seconds-to-low-minutes, not the half-gig
monsters. Languages drive which oracle generates the gold and which task kinds
apply. `superset` is the polyglot target that carries the cross-language tasks
(graphlens's headline capability).

To re-pin a tag you MUST regenerate gold (`scripts/build_gold.py`): symbols move
between releases.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from bench.config import TARGETS_DIR

if TYPE_CHECKING:
    import pathlib


@dataclass(frozen=True)
class Project:
    key: str  # slug: result/task filenames, chart labels
    repo: str  # "owner/name" on github.com
    tag: str  # immutable git tag — gold is verified at this tag
    languages: tuple[str, ...]  # drives oracle + applicable task kinds
    # Subpath the MCP servers analyze. Now always "." — graphlens identifies a
    # project by its git remote and indexes the whole repository, so a subpath
    # arm would be measuring a different corpus than the others. Kept as a field
    # (not deleted) because analyze_path is threaded through every arm.
    subdir: str = "."
    notes: str = ""

    @property
    def path(self) -> pathlib.Path:
        return TARGETS_DIR / self.key

    @property
    def analyze_path(self) -> pathlib.Path:
        return self.path if self.subdir == "." else self.path / self.subdir

    @property
    def clone_url(self) -> str:
        return f"https://github.com/{self.repo}"


PROJECTS: dict[str, Project] = {
    # --- Go ---
    "gin": Project(
        key="gin",
        repo="gin-gonic/gin",
        tag="v1.10.0",
        languages=("go",),
        notes="Go HTTP framework — middleware chains, interface impls, real call graph.",
    ),
    "echo": Project(
        key="echo",
        repo="labstack/echo",
        tag="v4.12.0",
        languages=("go",),
        notes="Go HTTP framework — Context interface + impl, middleware, Binder/Router.",
    ),
    # --- Rust ---
    "ripgrep": Project(
        key="ripgrep",
        repo="BurntSushi/ripgrep",
        tag="14.1.1",
        languages=("rust",),
        notes="Rust CLI — multi-crate workspace, traits, generics.",
    ),
    "clap": Project(
        key="clap",
        repo="clap-rs/clap",
        tag="v4.5.20",
        languages=("rust",),
        notes="Rust arg parser — derive macros, builder traits, multi-crate.",
    ),
    # --- Python ---
    "fastapi": Project(
        key="fastapi",
        repo="fastapi/fastapi",
        tag="0.115.0",
        languages=("python",),
        notes="Python framework — Pydantic models, dependency injection, decorators.",
    ),
    "click": Project(
        key="click",
        repo="pallets/click",
        tag="8.1.7",
        languages=("python",),
        notes="Python CLI toolkit — decorators, Command/Context classes.",
    ),
    "httpx": Project(
        key="httpx",
        repo="encode/httpx",
        tag="0.27.2",
        languages=("python",),
        notes="Python HTTP client — sync/async Client classes, transports.",
    ),
    # --- TypeScript ---
    "hono": Project(
        key="hono",
        repo="honojs/hono",
        tag="v4.6.0",
        languages=("typescript",),
        notes="TypeScript web framework — generics-heavy, middleware, type-level routing.",
    ),
    "zod": Project(
        key="zod",
        repo="colinhacks/zod",
        tag="v3.23.8",
        languages=("typescript",),
        notes="TypeScript schema validation — class hierarchy, generics.",
    ),
    "superset": Project(
        key="superset",
        repo="apache/superset",
        tag="6.0.0",
        languages=("python", "typescript"),
        notes="Polyglot Py+TS — carries cross-language (TS hook -> Python route handler) tasks.",
    ),
}

# Languages we know how to generate oracle gold for.
SUPPORTED_LANGUAGES: frozenset[str] = frozenset(
    {"go", "rust", "python", "typescript"}
)


def projects_for(keys: list[str] | None = None) -> list[Project]:
    if not keys:
        return list(PROJECTS.values())
    return [PROJECTS[k] for k in keys]
