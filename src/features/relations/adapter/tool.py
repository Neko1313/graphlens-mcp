from features.relations import service
from shared.common.db.graph import get_graph_store
from shared.common.db.registry import get_registry_store, resolve_project
from shared.common.indexing import resolve_symbol

__all__ = ["relations"]


async def relations(
    symbol: str,
    project: str | None = None,
    depth: int = 2,
    limit: int = 25,
    kinds: str = "",
    file: str = "",
) -> dict[str, object]:
    """Find a symbol's callers, callees, implementors, and references.

    ``symbol`` is a node id or a symbol name. Returns the four navigation
    groups to ``depth`` hops with ``*_total`` counts (and
    ``callees_unresolved`` for calls graphlens couldn't bind). ``kinds``
    (e.g. ``calls,references``) narrows which groups are computed. An ambiguous
    name returns candidates — narrow with ``file``. Pass ``project`` when more
    than one is indexed.
    """
    graph_store = get_graph_store()
    project_id = await resolve_project(get_registry_store(), project)
    node_id, candidates = await resolve_symbol(
        graph_store, project_id, symbol, file,
    )
    if node_id is None:
        status = "ambiguous" if candidates else "not_found"
        return {"status": status, "symbol": symbol, "candidates": candidates}
    result = await service.get_relations(
        graph_store, project_id, node_id, depth, limit, kinds,
    )
    if result is None:
        return {"status": "not_found", "symbol": symbol, "candidates": []}
    return result.model_dump()
