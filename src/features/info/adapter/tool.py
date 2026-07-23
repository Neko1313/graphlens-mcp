from entities.request import InfoParams
from entities.result import InfoResult, NotFound
from features.info import service
from shared.common.db.graph import get_graph_store
from shared.common.db.registry import get_registry_store, resolve_project
from shared.common.indexing import resolve_point

__all__ = ["info"]


async def info(params: InfoParams) -> InfoResult:
    """Read a symbol's source, or a file's outline/source, from the graph.

    A symbol returns its source + signature + metadata; a file returns its
    outline of symbols (the default) or full source + importers. An ambiguous
    name returns candidates. Does NOT search by meaning — use ``search`` for
    that.

    Pass ``ref``/``at`` to look the target up at an indexed commit instead of
    now. Only the graph is versioned, so a past revision returns the symbol's
    recorded shape (name, kind, file, metadata) without its body — the result
    carries a ``revision`` saying so.
    """
    registry = get_registry_store()
    graph_store = get_graph_store()
    project_id = await resolve_project(registry, params.project)

    if params.ref or params.at:
        point = await resolve_point(
            graph_store, project_id, params.ref, params.at,
        )
        if point is None:
            return NotFound(target=f"{params.ref or 'HEAD'}@{params.at}")
        historical = await service.info_at(
            graph_store, project_id, point, params.target, params.file,
        )
        if historical is not None:
            return historical
        return NotFound(target=params.target)

    result = await service.info(
        graph_store,
        registry,
        project_id,
        params.target,
        params.mode,
        params.limit,
        params.offset,
        params.file,
    )
    return result if result is not None else NotFound(target=params.target)
