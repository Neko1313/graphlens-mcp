from typing import Any

from shared.common.db.graph import GraphExecutor
from shared.common.indexing import temporal

__all__ = ["resolve_symbol", "resolve_symbol_at"]

_DEFINITION_KINDS = frozenset({"class", "function", "method"})
# An ``external_symbol`` is an unresolved reference (a phantom with no body and
# no callers) and an ``import`` is a statement, not a definition. Both share
# the imported symbol's name, so resolving "parseBody" would otherwise hand
# back the phantom and every import beside the real function — noise a weak
# model then spirals on. Drop them from candidates, keeping them only as a
# last resort when nothing else carries the name (a lone external symbol still
# resolves).
_NOISE_KINDS = frozenset({"external_symbol", "import"})
_CANDIDATE_LIMIT = 25


def _pick(
    candidates: list[dict[str, Any]],
) -> tuple[str | None, list[dict[str, Any]]]:
    """One candidate wins outright, or the whole list goes back to the caller.

    Prefer a lone definition over its imports/references of the same name, and
    drop phantom/import noise so an ambiguous list holds only real symbols.
    """
    signal = [c for c in candidates if c["kind"] not in _NOISE_KINDS]
    pool = signal or candidates
    if len(pool) == 1:
        return str(pool[0]["id"]), []
    defs = [c for c in pool if c["kind"] in _DEFINITION_KINDS]
    if len(defs) == 1:
        return str(defs[0]["id"]), []
    return None, pool


async def resolve_symbol_at(
    graph_store: GraphExecutor,
    project_id: str,
    point: temporal.Point,
    target: str,
    file: str = "",
) -> tuple[str | None, list[dict[str, Any]]]:
    """Resolve a target against the node set of a past point in time.

    Same contract as ``resolve_symbol``, read from the version log instead of
    the live graph — a symbol that has since been renamed or deleted still
    resolves at the revision where it existed.
    """
    rows = await temporal.state_at(
        graph_store,
        project_id,
        point.ref,
        point.seq,
        [target],
    )
    if rows:
        return target, []
    live = await temporal.state_at(
        graph_store,
        project_id,
        point.ref,
        point.seq,
    )
    candidates = [
        {
            "id": row["node_id"],
            "name": row["name"],
            "qualified_name": row["qualified_name"],
            "kind": row["kind"],
            "file_path": row["file_path"],
        }
        for row in live
        if row["name"] == target and (not file or row["file_path"] == file)
    ]
    return _pick(candidates[:_CANDIDATE_LIMIT])


async def resolve_symbol(
    graph_store: GraphExecutor,
    project_id: str,
    target: str,
    file: str = "",
) -> tuple[str | None, list[dict[str, object]]]:
    """Resolve a target to a node id, or a candidate list when ambiguous.

    Returns ``(node_id, [])`` when ``target`` is a node id or resolves to a
    single symbol; ``(None, candidates)`` otherwise. ``file`` narrows a name
    match to one file when several symbols share the name.
    """
    rows = await graph_store.execute(
        "MATCH (n:CodeNode {project_id: $p, local_id: $t}) "
        "RETURN n.local_id AS id LIMIT 1",
        {"p": project_id, "t": target},
    )
    if rows:
        return target, []
    params: dict[str, object] = {"p": project_id, "t": target}
    file_clause = ""
    if file:
        file_clause = " AND n.file_path = $f"
        params["f"] = file
    candidates = await graph_store.execute(
        "MATCH (n:CodeNode {project_id: $p}) "
        f"WHERE n.name = $t{file_clause} "
        "RETURN n.local_id AS id, n.name AS name, "
        "n.qualified_name AS qualified_name, n.kind AS kind, "
        f"n.file_path AS file_path LIMIT {_CANDIDATE_LIMIT}",
        params,
    )
    return _pick(candidates)
