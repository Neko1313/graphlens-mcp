from shared.common.db.graph import GraphStore

__all__ = ["resolve_symbol"]

_DEFINITION_KINDS = frozenset({"class", "function", "method"})


async def resolve_symbol(
    graph_store: GraphStore,
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
        "n.file_path AS file_path LIMIT 25",
        params,
    )
    if len(candidates) == 1:
        return str(candidates[0]["id"]), []
    # Prefer a lone definition over its imports/references of the same name.
    defs = [c for c in candidates if c["kind"] in _DEFINITION_KINDS]
    if len(defs) == 1:
        return str(defs[0]["id"]), []
    return None, candidates
