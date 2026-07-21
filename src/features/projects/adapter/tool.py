import asyncio
from pathlib import Path

from mcp.server.mcpserver import (
    AcceptedElicitation,
    Context,
    DeclinedElicitation,
)
from pydantic import BaseModel, Field

from features.projects import service
from shared.common.context import AppContext

__all__ = [
    "index_project",
    "list_projects",
    "refresh_project",
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


async def index_project(
    path: str,
    ctx: Context[AppContext],
    name: str | None = None,
    description: str | None = None,
    subpaths: list[str] | None = None,
) -> dict[str, object]:
    """Index a project directory into the code graph so it can be searched.

    Parses the project with graphlens, stores its symbols and relations, and
    embeds functions/classes/methods for semantic search. Reports progress as
    it works, and asks the user to confirm (and optionally name/describe the
    project) when the client supports it. Re-running on the same path refreshes
    that project's index in place.

    Pass ``path`` (a directory). ``name`` and ``description`` are optional —
    the name defaults to the directory name. ``subpaths`` restricts indexing to
    those subdirectories (monorepo scope). Does NOT search the code; use the
    search tools for that.
    """
    app = ctx.request_context.lifespan_context
    root = Path(path).expanduser()
    if not root.is_dir():
        msg = f"not a directory: {path}"
        raise ValueError(msg)

    project = await asyncio.to_thread(
        service.build_project, root, name, description,
    )

    caps = ctx.client_capabilities
    if getattr(caps, "elicitation", None) is not None:
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
            answer = None
        if isinstance(answer, AcceptedElicitation):
            if not answer.data.confirm:
                return {"status": "skipped", "reason": "not confirmed"}
            project = await asyncio.to_thread(
                service.build_project,
                root,
                answer.data.name.strip() or project.name,
                answer.data.description.strip() or project.description,
            )
        elif isinstance(answer, DeclinedElicitation):
            return {"status": "declined"}
        elif answer is not None:
            return {"status": "cancelled"}

    async def on_progress(done: float, total: float, message: str) -> None:
        await ctx.report_progress(done, total, message)

    result = await service.index_project(
        project,
        app.graph_store,
        app.vector_store,
        app.registry,
        on_progress,
        subpaths,
    )
    return {
        "status": "indexed",
        "project": project.model_dump(mode="json"),
        "languages": result.languages,
        "files": result.files,
        "nodes": result.nodes,
        "relations": result.relations,
        "embedded": result.embedded,
        "resolver_status": result.resolver_status,
    }


async def list_projects(ctx: Context[AppContext]) -> list[dict[str, object]]:
    """List every indexed project: id, name, path, git url, description."""
    app = ctx.request_context.lifespan_context
    projects = await service.list_projects(app.registry)
    return [project.model_dump(mode="json") for project in projects]


async def refresh_project(
    project: str,
    ctx: Context[AppContext],
) -> dict[str, object]:
    """Re-index an already-registered project from its stored path.

    Picks up file edits since the last index. Pass ``project`` (the id).
    Reports progress as it works.
    """
    app = ctx.request_context.lifespan_context

    async def on_progress(done: float, total: float, message: str) -> None:
        await ctx.report_progress(done, total, message)

    result = await service.refresh_project(
        app.graph_store, app.vector_store, app.registry, project, on_progress,
    )
    if result is None:
        return {"status": "not_found", "project": project}
    return {
        "status": "refreshed",
        "project": project,
        "files": result.files,
        "nodes": result.nodes,
        "relations": result.relations,
        "embedded": result.embedded,
        "resolver_status": result.resolver_status,
    }


async def remove_project(
    project: str,
    ctx: Context[AppContext],
) -> dict[str, object]:
    """Remove a project from the index — permanently.

    Deletes the project's code graph, its embeddings, and its registry entry
    (so it stops appearing in the project list and search). Pass ``project``
    (the project id). Asks the user to confirm when the client supports it.
    """
    app = ctx.request_context.lifespan_context
    existing = await service.get_project(app.registry, project)
    if existing is None:
        return {"status": "not_found", "project": project}

    caps = ctx.client_capabilities
    if getattr(caps, "elicitation", None) is not None:
        try:
            answer = await ctx.elicit(
                message=(
                    f"Delete the index for {existing.name} ({project})?\n"
                    f"Path: {existing.path}\nThis cannot be undone."
                ),
                schema=_RemoveConfirmation,
            )
        except Exception:
            answer = None
        if isinstance(answer, AcceptedElicitation):
            if not answer.data.confirm:
                return {"status": "skipped", "reason": "not confirmed"}
        elif isinstance(answer, DeclinedElicitation):
            return {"status": "declined"}
        elif answer is not None:
            return {"status": "cancelled"}

    await service.remove_project(
        app.graph_store, app.vector_store, app.registry, project,
    )
    return {"status": "removed", "project": existing.model_dump(mode="json")}
