from entities.request import RelationsParams
from entities.result import Candidates, NodeRef, NotFound, RelationsLookup
from features.relations import service
from shared.common.db.graph import get_graph_store
from shared.common.db.registry import get_registry_store, resolve_project
from shared.common.indexing import (
    resolve_point,
    resolve_symbol,
    resolve_symbol_at,
)

__all__ = ["relations"]


async def relations(params: RelationsParams) -> RelationsLookup:
    """Find a symbol's callers, callees, implementors, and references.

    Returns the four navigation groups to ``depth`` hops with ``*_total``
    counts (and ``callees_unresolved`` for calls graphlens couldn't bind). An
    ambiguous name returns candidates — narrow with ``file``.

    Pass ``ref``/``at`` to read the graph as it was at an indexed commit
    instead of now: neighbours then come from the version log, so an edge that
    has since been removed is still there and one added later is not.
    """
    graph_store = get_graph_store()
    project_id = await resolve_project(get_registry_store(), params.project)
    point = None
    if params.ref or params.at:
        point = await resolve_point(
            graph_store,
            project_id,
            params.ref,
            params.at,
        )
        if point is None:
            return NotFound(target=f"{params.ref or 'HEAD'}@{params.at}")

    if point is None:
        node_id, candidates = await resolve_symbol(
            graph_store,
            project_id,
            params.symbol,
            params.file,
        )
    else:
        node_id, candidates = await resolve_symbol_at(
            graph_store,
            project_id,
            point,
            params.symbol,
            params.file,
        )
    if node_id is None:
        if candidates:
            return Candidates(
                candidates=[NodeRef.model_validate(c) for c in candidates],
            )
        return NotFound(target=params.symbol)

    if point is None:
        result = await service.get_relations(
            graph_store,
            project_id,
            node_id,
            params.depth,
            params.limit,
            params.kinds,
        )
    else:
        result = await service.get_relations_at(
            graph_store,
            project_id,
            point,
            node_id,
            params.depth,
            params.limit,
            params.kinds,
        )
    return result if result is not None else NotFound(target=params.symbol)
