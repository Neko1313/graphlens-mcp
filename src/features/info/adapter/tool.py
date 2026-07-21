from entities.request import InfoParams
from entities.result import InfoResult, NotFound
from features.info import service
from shared.common.db.graph import get_graph_store
from shared.common.db.registry import get_registry_store, resolve_project

__all__ = ["info"]


async def info(params: InfoParams) -> InfoResult:
    """Read a symbol's source, or a file's outline/source, from the graph.

    A symbol returns its source + signature + metadata; a file returns its
    outline of symbols (the default) or full source + importers. An ambiguous
    name returns candidates. Does NOT search by meaning — use ``search`` for
    that.
    """
    registry = get_registry_store()
    project_id = await resolve_project(registry, params.project)
    result = await service.info(
        get_graph_store(),
        registry,
        project_id,
        params.target,
        params.mode,
        params.limit,
        params.offset,
        params.file,
    )
    return result if result is not None else NotFound(target=params.target)
