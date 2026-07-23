import asyncio
import hashlib
import re
import shutil
from pathlib import Path
from urllib.parse import urlsplit

from entities.project import Project
from features.projects.git import (
    clone_into,
    get_remote_url,
    head_commit,
    ls_remote,
    new_checkout_dir,
    remove_retained,
    retain_checkout,
)
from shared.common.context import AppContext
from shared.common.db.graph import GraphStore
from shared.common.db.registry import ProjectRegistry
from shared.common.db.vector import VectorStore
from shared.common.indexing import (
    IndexResult,
    ProgressCallback,
    index_project_graph,
    persist,
    temporal,
)
from shared.common.setting.const import CODE_COLLECTION

__all__ = [
    "NoRemoteError",
    "build_project",
    "compute_project_id",
    "get_project",
    "index_project",
    "index_remote",
    "list_projects",
    "normalize_remote",
    "remote_head",
    "remove_project",
]

_SLUG_RE = re.compile(r"[^0-9A-Za-z]+")
_MAX_SLUG = 40

# scp-style remote [user@]host:path: no scheme, colon splits host.
_SCP_RE = re.compile(r"^(?:[^@/]+@)?(?P<host>[^/:]+):(?P<path>.+)$")


class NoRemoteError(ValueError):
    """Raised when a project has no git remote to derive its identity from.

    Identity is ``hash(git remote)`` so the same repo maps to one
    project across a dev clone and a CI clone. A repo with no remote
    (un-pushed, or a non-git directory) has no stable identity and is not
    indexable.
    """


def normalize_remote(url: str) -> str:
    """Canonicalize a git remote URL to ``host/owner/repo`` (lowercased).

    The point is that every spelling of the *same* remote collapses to one
    identity — so a dev clone over ssh and a server's CI clone over https index
    into the same project. So this drops the scheme, any ``user[:token]@``
    userinfo, the port, a trailing ``.git``, and surrounding slashes, then
    lowercases the whole thing.

    Lowercasing owner/repo (not just the DNS host) is deliberate: GitHub/GitLab
    treat them case-insensitively, and matching dev↔CI matters more than the
    negligible risk of two repos differing only by case on a case-sensitive
    host.
    """
    raw = url.strip()
    if "://" in raw:
        parsed = urlsplit(raw)
        host = (parsed.hostname or "").lower()
        path = parsed.path
    else:
        match = _SCP_RE.match(raw)
        if match is not None:
            host = match.group("host").lower()
            path = match.group("path")
        else:
            host, path = "", raw
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    canonical = f"{host}/{path}" if host else path
    return canonical.strip("/").lower()


def compute_project_id(remote: str) -> str:
    """A stable project id derived from ``hash(remote)``.

    Two clones of the same repo (at any on-disk path) map to the same id — the
    intended server-side dedup. A repository is indexed whole, so the remote is
    the only identity input: there is no per-subtree project. The result keeps
    a readable ``{slug}_{digest}`` shape and an ``[A-Za-z0-9_]``-only charset
    starting with a letter/underscore, so it is valid as both a graph filter
    value and a Milvus scalar.
    """
    canonical = normalize_remote(remote)
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:12]
    repo = canonical.rsplit("/", 1)[-1] or canonical
    slug = _SLUG_RE.sub("_", repo).strip("_").lower()[:_MAX_SLUG]
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
    """Assemble a Project from a checkout, deriving identity from its remote.

    Raises ``NoRemoteError`` when ``root`` has no git remote — identity needs
    one, so un-pushed/non-git directories are not indexable.
    """
    resolved = root.resolve()
    remote = get_remote_url(resolved)
    if remote is None:
        msg = (
            f"{resolved} has no git remote — project identity is "
            "hash(remote), so add a remote (or push the repo) first. "
            "Un-pushed or non-git directories are not indexable."
        )
        raise NoRemoteError(msg)
    return Project(
        id=compute_project_id(remote),
        name=name or resolved.name,
        path=resolved,
        description=description or None,
        git_url=remote,
    )


async def index_project(
    project: Project,
    app: AppContext,
    on_progress: ProgressCallback | None = None,
) -> IndexResult:
    """Index the project's code graph + embeddings, then register it.

    Registration happens last so a failed index (e.g. a path with no
    supported languages) never leaves a phantom entry in the registry. The
    whole repository is indexed; the checkout's HEAD is captured so the run is
    recorded in the temporal log.
    """
    commit = await asyncio.to_thread(head_commit, project.path)
    result = await index_project_graph(
        project.path,
        project.id,
        app.graph_store,
        app.vector_store,
        on_progress,
        commit,
    )
    await app.registry.add(project)
    return result


async def remote_head(
    repo_url: str,
    ref: str | None,
    ci_token: str | None,
    graph_store: GraphStore,
) -> tuple[str, str, str] | None:
    """``(project_id, ref, sha)`` if the remote ref's HEAD is already indexed.

    The no-op fast path: resolve the ref via ``git ls-remote`` and compare it
    to the sha the project's graph currently reflects, so an unchanged repo
    never pays for a clone. Gated on the graph head (not "sha ever indexed on
    any ref"): the graph is a single latest snapshot, so after another ref was
    indexed it holds that ref's sha and this one must be re-indexed. Only runs
    when ``ref`` is given; otherwise, or when the sha differs, returns
    ``None``.
    """
    if not ref:
        return None
    sha = await asyncio.to_thread(ls_remote, repo_url, ref, ci_token)
    if sha is None:
        return None
    project_id = compute_project_id(repo_url)
    if await persist.graph_head(graph_store, project_id) == sha:
        return project_id, ref, sha
    return None


async def index_remote(
    repo_url: str,
    ref: str | None,
    ci_token: str | None,
    app: AppContext,
    on_progress: ProgressCallback | None = None,
) -> tuple[Project, IndexResult]:
    """Clone ``repo_url@ref``, index it, and retain the tree for reads.

    Identity comes from the clone's remote (which is ``repo_url``), so a
    server-side CI clone and a developer's clone map to the same project. Name
    and description default from the repo (server mode is
    non-interactive). The clone lands in a pid-named dir (removed in a
    ``finally`` so a cancelled clone can't leak it); on success it is moved to
    a stable per-project tree that the read paths use, since indexing deletes
    the checkout otherwise and disk-backed reads would then fail.
    """
    dest = new_checkout_dir()
    try:
        ok = await asyncio.to_thread(clone_into, repo_url, ref, ci_token, dest)
        if not ok:
            detail = f" (ref {ref})" if ref else ""
            msg = f"failed to clone {repo_url}{detail}"
            raise ValueError(msg)
        project = await asyncio.to_thread(build_project, dest)
        stable = await asyncio.to_thread(retain_checkout, dest, project.id)
        project = project.model_copy(update={"path": stable})
        result = await index_project(project, app, on_progress)
    finally:
        await asyncio.to_thread(shutil.rmtree, dest, ignore_errors=True)
    return project, result


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
    """Delete a project's graph nodes, vectors, history, and registry entry.

    Vectors are cleared by filter from the shared collection (not by dropping a
    collection), matching the filter-not-boundary isolation model.
    """
    await persist.clear_project(graph_store, project_id)
    await temporal.clear_project(graph_store, project_id)
    await vector_store.delete(
        CODE_COLLECTION, persist.vector_project_filter(project_id),
    )
    await asyncio.to_thread(remove_retained, project_id)
    await registry.remove(project_id)
