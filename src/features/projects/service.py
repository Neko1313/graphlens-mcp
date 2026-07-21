import hashlib
import re
from pathlib import Path

from entities.project import Project
from features.projects.git import get_remote_url
from shared.common.db.graph import GraphStore
from shared.common.db.registry import ProjectRegistry
from shared.common.db.vector import VectorStore
from shared.common.indexing import (
    IndexResult,
    ProgressCallback,
    index_project_graph,
    persist,
)

__all__ = [
    "build_project",
    "compute_project_id",
    "get_project",
    "index_project",
    "list_projects",
    "refresh_project",
    "remove_project",
]

_SLUG_RE = re.compile(r"[^0-9A-Za-z]+")
_MAX_SLUG = 40


def compute_project_id(root: Path) -> str:
    """A stable id derived from the resolved absolute path.

    Path-based (not git-url-based): the same checkout must map to the same
    index across re-runs, one remote can back several indexable subtrees, and
    two clones of one repo are two projects. The result is a short ASCII token
    that starts with a letter/underscore — valid as both a vector collection
    name and a store key.
    """
    resolved = root.resolve()
    digest = hashlib.sha256(str(resolved).encode()).hexdigest()[:12]
    slug = _SLUG_RE.sub("_", resolved.name).strip("_").lower()[:_MAX_SLUG]
    if not slug:
        slug = "project"
    project_id = f"{slug}_{digest}"
    if not (project_id[0].isalpha() or project_id[0] == "_"):
        project_id = f"p_{project_id}"
    return project_id


def build_project(
    root: Path,
    name: str | None = None,
    description: str | None = None,
) -> Project:
    """Assemble a Project from a path, filling in id, name, and git url."""
    resolved = root.resolve()
    return Project(
        id=compute_project_id(resolved),
        name=name or resolved.name,
        path=resolved,
        description=description or None,
        git_url=get_remote_url(resolved),
    )


async def index_project(
    project: Project,
    graph_store: GraphStore,
    vector_store: VectorStore,
    registry: ProjectRegistry,
    on_progress: ProgressCallback | None = None,
    subpaths: list[str] | None = None,
) -> IndexResult:
    """Index the project's code graph + embeddings, then register it.

    Registration happens last so a failed index (e.g. a path with no
    supported languages) never leaves a phantom entry in the registry.
    ``subpaths`` restricts indexing to those subdirectories.
    """
    result = await index_project_graph(
        project.path,
        project.id,
        graph_store,
        vector_store,
        on_progress,
        subpaths,
    )
    await registry.add(project)
    return result


async def refresh_project(
    graph_store: GraphStore,
    vector_store: VectorStore,
    registry: ProjectRegistry,
    project_id: str,
    on_progress: ProgressCallback | None = None,
) -> IndexResult | None:
    """Re-index an already-registered project from its stored path.

    None if the project isn't registered.
    """
    project = await registry.get(project_id)
    if project is None:
        return None
    return await index_project_graph(
        project.path,
        project.id,
        graph_store,
        vector_store,
        on_progress,
    )


async def list_projects(registry: ProjectRegistry) -> list[Project]:
    return await registry.list_all()


async def get_project(
    registry: ProjectRegistry,
    project_id: str,
) -> Project | None:
    return await registry.get(project_id)


async def remove_project(
    graph_store: GraphStore,
    vector_store: VectorStore,
    registry: ProjectRegistry,
    project_id: str,
) -> None:
    """Delete a project's graph nodes, vectors, and registry entry."""
    await persist.clear_project(graph_store, project_id)
    await vector_store.drop_collection(project_id)
    await registry.remove(project_id)
