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

    Raises ValueError when the id is unknown, none are indexed, or the choice
    is ambiguous — surfacing an actionable message to the caller.
    """
    if project_id:
        if await registry.get(project_id) is None:
            msg = f"unknown project: {project_id}"
            raise ValueError(msg)
        return project_id
    projects = await registry.list_all()
    if len(projects) == 1:
        return projects[0].id
    if not projects:
        msg = "no projects indexed yet; run index_project first"
        raise ValueError(msg)
    ids = ", ".join(p.id for p in projects)
    msg = f"multiple projects indexed; pass project= one of: {ids}"
    raise ValueError(msg)
