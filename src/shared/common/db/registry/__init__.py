from functools import cache

from shared.common.db.registry.kuzu import KuzuProjectRegistry
from shared.common.db.registry.port import ProjectRegistry
from shared.common.setting.getter_setting import get_registry_db_path

__all__ = [
    "ProjectRegistry",
    "get_registry_store",
    "resolve_project",
]


@cache
def get_registry_store() -> ProjectRegistry:
    """Open the global project registry.

    Always local metadata (an embedded Kuzu ``registry.db``) — independent of
    which backend serves the per-project code graph.
    """
    return KuzuProjectRegistry.open(get_registry_db_path())


async def resolve_project(
    registry: ProjectRegistry,
    project_id: str | None,
) -> str:
    """Pick which project a query targets: the given id, or the sole one.

    An id like ``hono_fe98d8970535`` is not something a caller can know, so a
    project's **name** resolves too, as does an unambiguous prefix of its id.
    Insisting on the exact id turned every guess into a rejected call followed
    by list_projects and a retry — three round trips to say "hono".

    Raises ValueError when nothing matches, none are indexed, or the choice is
    ambiguous — surfacing an actionable message to the caller.
    """
    projects = await registry.list_all()
    if project_id:
        if await registry.get(project_id) is None:
            # With one project indexed there is nothing else the caller could
            # have meant, so a wrong value is answered rather than rejected —
            # models fill this argument from whatever name is in front of them
            # (a crate, a package, a directory) and a rejection buys three
            # round trips to arrive back at the only possible answer.
            if len(projects) == 1:
                return projects[0].id
            return _match(project_id, projects)
        return project_id
    if len(projects) == 1:
        return projects[0].id
    if not projects:
        msg = "no projects indexed yet; run index first"
        raise ValueError(msg)
    listing = ", ".join(f"{p.name} ({p.id})" for p in projects)
    msg = f"multiple projects indexed; pass project= one of: {listing}"
    raise ValueError(msg)


def _match(given: str, projects: list) -> str:
    """Resolve a name or id-prefix to exactly one project id."""
    needle = given.strip().lower()
    hits = [
        p
        for p in projects
        if p.name.lower() == needle or p.id.lower().startswith(needle)
    ]
    if len(hits) == 1:
        return hits[0].id
    known = ", ".join(f"{p.name} ({p.id})" for p in projects) or "none"
    if not hits:
        msg = f"unknown project: {given}. Indexed: {known}"
        raise ValueError(msg)
    msg = f"ambiguous project: {given}. Matches: {known}"
    raise ValueError(msg)
