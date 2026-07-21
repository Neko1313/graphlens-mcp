from entities.result import NodeRef, RelationsResult
from shared.common.db.graph import GraphStore

__all__ = ["get_relations"]

_MAX_DEPTH = 5
_MAX_LIMIT = 200
_UNRESOLVED_KIND = "external_symbol"
# The three edge kinds behind the four navigation groups (calls splits into
# callers + callees by direction).
_GROUP_KINDS = ("calls", "inherits_from", "references")
_MEMBER_FIELDS = (
    "m.local_id AS id, m.name AS name, m.qualified_name AS qualified_name, "
    "m.kind AS kind, m.file_path AS file_path"
)


def _wanted_kinds(kinds: str) -> set[str]:
    requested = {k.strip() for k in kinds.split(",") if k.strip()}
    valid = requested & set(_GROUP_KINDS)
    return valid or set(_GROUP_KINDS)


def _pattern(direction: str, depth: int) -> str:
    anchor = "(n:CodeNode {project_id: $p, local_id: $id})"
    other = "(m:CodeNode)"
    edge = "-[r:Rel]->" if depth == 1 else f"-[r:Rel*1..{depth}]->"
    return (
        f"{anchor}{edge}{other}"
        if direction == "out"
        else f"{other}{edge}{anchor}"
    )


def _kind_cond(kind: str, depth: int) -> str:
    # kind is one of _GROUP_KINDS (fixed vocabulary), so inlining is safe —
    # and Kuzu rejects a parameter inside a recursive rel filter anyway.
    if depth == 1:
        return f"r.kind = '{kind}'"
    return f"all(x IN rels(r) WHERE x.kind = '{kind}')"


async def _node_ref(
    graph_store: GraphStore,
    project_id: str,
    node_id: str,
) -> NodeRef | None:
    rows = await graph_store.execute(
        "MATCH (n:CodeNode {project_id: $p, local_id: $id}) "
        "RETURN n.local_id AS id, n.name AS name, "
        "n.qualified_name AS qualified_name, n.kind AS kind, "
        "n.file_path AS file_path LIMIT 1",
        {"p": project_id, "id": node_id},
    )
    return NodeRef(**rows[0]) if rows else None


async def _group(
    graph_store: GraphStore,
    project_id: str,
    node_id: str,
    direction: str,
    kind: str,
    depth: int,
    limit: int,
) -> tuple[list[NodeRef], int, int]:
    """Return (resolved members, resolved total, unresolved total) for one
    direction + edge kind. Unresolved = ``external_symbol`` targets.
    """
    pattern = _pattern(direction, depth)
    cond = _kind_cond(kind, depth)
    params = {"p": project_id, "id": node_id}
    resolved = f"{cond} AND m.kind <> '{_UNRESOLVED_KIND}'"
    items = await graph_store.execute(
        f"MATCH {pattern} WHERE {resolved} "
        f"RETURN DISTINCT {_MEMBER_FIELDS} LIMIT {limit}",
        params,
    )
    total = await graph_store.execute(
        f"MATCH {pattern} WHERE {resolved} RETURN count(DISTINCT m) AS c",
        params,
    )
    unresolved = await graph_store.execute(
        f"MATCH {pattern} WHERE {cond} AND m.kind = '{_UNRESOLVED_KIND}' "
        "RETURN count(DISTINCT m) AS c",
        params,
    )
    members = [NodeRef(**row) for row in items]
    return members, total[0]["c"], unresolved[0]["c"]


async def get_relations(
    graph_store: GraphStore,
    project_id: str,
    node_id: str,
    depth: int = 2,
    limit: int = 25,
    kinds: str = "",
) -> RelationsResult | None:
    """The four navigation groups for a node. None if the node is unknown.

    callers/callees follow ``calls`` to ``depth`` hops; implementors follow
    ``inherits_from`` and references follow ``references`` (direct). ``kinds``
    narrows which groups are computed; ``*_total`` is the true count before
    ``limit``, ``callees_unresolved`` counts calls graphlens couldn't bind.
    """
    node = await _node_ref(graph_store, project_id, node_id)
    if node is None:
        return None
    depth = max(1, min(depth, _MAX_DEPTH))
    limit = max(1, min(limit, _MAX_LIMIT))
    wanted = _wanted_kinds(kinds)
    result = RelationsResult(node=node, depth=depth, kinds=sorted(wanted))
    if "calls" in wanted:
        result.callees, result.callees_total, result.callees_unresolved = (
            await _group(
                graph_store, project_id, node_id, "out", "calls", depth, limit,
            )
        )
        result.callers, result.callers_total, _ = await _group(
            graph_store, project_id, node_id, "in", "calls", depth, limit,
        )
    if "inherits_from" in wanted:
        result.implementors, result.implementors_total, _ = await _group(
            graph_store, project_id, node_id, "in", "inherits_from", 1, limit,
        )
    if "references" in wanted:
        result.references, result.references_total, _ = await _group(
            graph_store, project_id, node_id, "in", "references", 1, limit,
        )
    return result
