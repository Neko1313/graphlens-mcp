import asyncio
from pathlib import Path

from mcp.server.mcpserver import (
    AcceptedElicitation,
    Context,
    DeclinedElicitation,
)
from pydantic import BaseModel, Field

from entities.project import Project
from entities.request import IndexParams, RemoveProjectParams
from entities.result import (
    AlreadyCurrent,
    Cancelled,
    Declined,
    Indexed,
    IndexProjectResult,
    ProjectNotFound,
    Removed,
    RemoveProjectResult,
    Skipped,
)
from features.projects import service
from features.projects.adapter.resource import (
    notify_resources_changed,
    register_project_resource,
    unregister_project_resource,
)
from features.projects.git import sweep_stale_checkouts
from shared.common.context import AppContext

__all__ = [
    "index",
    "list_projects",
    "remove_project",
]


class _IndexConfirmation(BaseModel):
    """What the user fills in before a project is indexed."""

    confirm: bool = Field(
        default=True,
        description="Index this project now?",
    )
    name: str = Field(
        default="",
        description="Project name (leave blank to keep the detected name)",
    )
    description: str = Field(
        default="",
        description="Short description of the project (optional)",
    )


class _RemoveConfirmation(BaseModel):
    """The confirmation gate before a project's index is deleted."""

    confirm: bool = Field(
        default=False,
        description="Delete this project's index? This cannot be undone.",
    )


def _validated_root(path: str) -> Path:
    """Expand and validate the index path (blocking filesystem access)."""
    root = Path(path).expanduser()
    if not root.is_dir():
        msg = f"not a directory: {path}"
        raise ValueError(msg)
    return root


async def _confirm_local(
    ctx: Context[AppContext],
    project: Project,
    root: Path,
) -> Project | Skipped | Declined | Cancelled:
    """Interactive confirm for a local index.

    Returns the (possibly renamed) project to proceed, or a terminal outcome
    when the user declines/cancels. No-op (returns the project) when the client
    can't elicit — server/CI callers never see a prompt.
    """
    caps = ctx.client_capabilities
    if getattr(caps, "elicitation", None) is None:
        return project
    try:
        answer = await ctx.elicit(
            message=(
                f"Index {project.name}?\n"
                f"Path: {project.path}\n"
                f"Git:  {project.git_url or '(none)'}\n"
                f"ID:   {project.id}"
            ),
            schema=_IndexConfirmation,
        )
    except Exception:
        return project
    if isinstance(answer, AcceptedElicitation):
        if not answer.data.confirm:
            return Skipped(reason="not confirmed")
        return await asyncio.to_thread(
            service.build_project,
            root,
            answer.data.name.strip() or project.name,
            answer.data.description.strip() or project.description,
        )
    if isinstance(answer, DeclinedElicitation):
        return Declined()
    return Cancelled() if answer is not None else project


async def index(
    params: IndexParams,
    ctx: Context[AppContext],
) -> IndexProjectResult:
    """Index a project into the code graph so it can be searched.

    Give exactly one source: ``directory`` (a local checkout) or ``repo_url``
    (a remote cloned to a temp dir, for server/CI use). Parses with graphlens,
    stores symbols and relations, embeds functions/classes/methods, and records
    the HEAD commit in the temporal log. Reports progress; in local mode it
    asks to confirm when the client supports it.

    Project identity is hash(git remote), NOT the on-disk path: two clones of
    the same repo are one project (re-running ``index`` refreshes it in place —
    there is no separate refresh tool). A project is always a whole repository;
    subtrees are not indexed separately. Requires a git remote. Does NOT search
    the code; use the search tools for that.
    """
    app = ctx.request_context.lifespan_context
    await asyncio.to_thread(sweep_stale_checkouts)

    async def on_progress(done: float, total: float, message: str) -> None:
        await ctx.report_progress(done, total, message)

    token = params.ci_token.get_secret_value() if params.ci_token else None

    if params.repo_url is not None:
        current = await service.remote_head(
            params.repo_url, params.ref, token, app.graph_store,
        )
        if current is not None:
            project_id, ref, sha = current
            # Only skip if the project is actually registered — a graph_head
            # that outlived a failed registry.add would otherwise strand an
            # unlisted, un-removable project; falling through re-registers it.
            registered = await service.get_project(app.registry, project_id)
            if registered is not None:
                return AlreadyCurrent(project=project_id, ref=ref, sha=sha)
        project, result = await service.index_remote(
            params.repo_url, params.ref, token, app, on_progress,
        )
    else:
        root = await asyncio.to_thread(_validated_root, params.directory or "")
        project = await asyncio.to_thread(
            service.build_project, root, params.name, params.description,
        )
        confirmed = await _confirm_local(ctx, project, root)
        if not isinstance(confirmed, Project):
            return confirmed
        project = confirmed
        result = await service.index_project(project, app, on_progress)

    register_project_resource(ctx.mcp_server, project)
    await notify_resources_changed(ctx)
    return Indexed(
        project=project,
        languages=result.languages,
        files=result.files,
        nodes=result.nodes,
        relations=result.relations,
        embedded=result.embedded,
        resolver_status=result.resolver_status,
        reused=result.reused,
        deleted=result.deleted,
        unresolved=result.unresolved,
        ref=result.ref,
        changes=result.changes,
    )


async def list_projects(ctx: Context[AppContext]) -> list[Project]:
    """List every indexed project: id, name, path, git url, description."""
    app = ctx.request_context.lifespan_context
    return await service.list_projects(app.registry)


async def remove_project(
    params: RemoveProjectParams,
    ctx: Context[AppContext],
) -> RemoveProjectResult:
    """Remove a project from the index — permanently.

    Deletes the project's code graph, its embeddings, and its registry entry
    (so it stops appearing in the project list and search). Asks the user to
    confirm when the client supports it.
    """
    app = ctx.request_context.lifespan_context
    existing = await service.get_project(app.registry, params.project)
    if existing is None:
        return ProjectNotFound(project=params.project)

    caps = ctx.client_capabilities
    if getattr(caps, "elicitation", None) is not None:
        try:
            pid = params.project
            answer = await ctx.elicit(
                message=(
                    f"Delete the index for {existing.name} ({pid})?\n"
                    f"Path: {existing.path}\nThis cannot be undone."
                ),
                schema=_RemoveConfirmation,
            )
        except Exception:
            answer = None
        if isinstance(answer, AcceptedElicitation):
            if not answer.data.confirm:
                return Skipped(reason="not confirmed")
        elif isinstance(answer, DeclinedElicitation):
            return Declined()
        elif answer is not None:
            return Cancelled()

    await service.remove_project(
        app.graph_store, app.vector_store, app.registry, params.project,
    )
    unregister_project_resource(ctx.mcp_server, params.project)
    await notify_resources_changed(ctx)
    return Removed(project=existing)
