from graphlens import RelationKind

from shared.common.db.graph import GraphStore

__all__ = ["get_relations", "resolve_kinds"]

_MAX_DEPTH = 5
_MAX_LIMIT = 200
_VALID_KINDS = frozenset(kind.value for kind in RelationKind)
# Default to the navigation-relevant edges; the structural ones (contains,
# declares, …) would just clutter callers/callees with parameters and owners.
_DEFAULT_KINDS: tuple[str, ...] = (
    "calls",
    "references",
    "inherits_from",
    "has_type",
)
_FIELDS = (
    "m.local_id AS id, m.name AS name, m.qualified_name AS qn, "
    "m.kind AS kind, m.file_path AS file_path"
)


def resolve_kinds(kinds: str) -> list[str]:
    """Parse + validate a ``kinds`` value, else fall back to the default."""
    requested = [k.strip() for k in kinds.split(",") if k.strip()]
    valid = [k for k in requested if k in _VALID_KINDS]
    return valid or list(_DEFAULT_KINDS)


def _kind_literal(kinds: list[str]) -> str:
    # kinds are validated against the RelationKind enum, so inlining them
    # (needed because Kuzu rejects a parameter inside a recursive rel filter)
    # is injection-safe.
    return "[" + ", ".join(f"'{kind}'" for kind in kinds) + "]"


def _match(direction: str, depth: int, kinds: list[str]) -> str:
    anchor = "(n:CodeNode {project_id: $p, local_id: $id})"
    other = "(m:CodeNode)"
    if depth == 1:
        pattern = (
            f"{anchor}-[r:Rel]->{other}"
            if direction == "out"
            else f"{other}-[r:Rel]->{anchor}"
        )
        clause = " WHERE r.kind IN $kinds"
    else:
        edge = f"-[r:Rel*1..{depth}]->"
        pattern = (
            f"{anchor}{edge}{other}"
            if direction == "out"
            else f"{other}{edge}{anchor}"
        )
        literal = _kind_literal(kinds)
        clause = f" WHERE all(x IN rels(r) WHERE x.kind IN {literal})"
    return f"MATCH {pattern}{clause}"


async def _neighbors(
    graph_store: GraphStore,
    project_id: str,
    node_id: str,
    direction: str,
    depth: int,
    limit: int,
    kinds: list[str],
) -> tuple[list[dict[str, object]], int]:
    base = _match(direction, depth, kinds)
    params: dict[str, object] = {"p": project_id, "id": node_id}
    if depth == 1:
        params["kinds"] = kinds
        # collect() groups by the node fields → one row per neighbour (with
        # its edge kinds), so len(items) matches count(DISTINCT m). Putting
        # r.kind in a plain DISTINCT would instead split a neighbour reached
        # by two kinds into two rows.
        items_query = (
            f"{base} RETURN {_FIELDS}, "
            f"collect(DISTINCT r.kind) AS rel_kinds LIMIT {limit}"
        )
    else:
        items_query = f"{base} RETURN DISTINCT {_FIELDS} LIMIT {limit}"
    items = await graph_store.execute(items_query, params)
    totals = await graph_store.execute(
        f"{base} RETURN count(DISTINCT m) AS c", params,
    )
    return items, (totals[0]["c"] if totals else 0)


async def get_relations(
    graph_store: GraphStore,
    project_id: str,
    node_id: str,
    depth: int = 2,
    limit: int = 25,
    kinds: str = "",
) -> dict[str, object] | None:
    """Callers and callees of a node, to ``depth`` hops. None if unknown.

    ``kinds`` (comma-separated relation kinds, e.g. ``calls,references``)
    narrows which edges are followed; unset uses the navigation defaults.
    ``*_total`` is the true neighbour count before ``limit``.
    """
    node_rows = await graph_store.execute(
        "MATCH (n:CodeNode {project_id: $p, local_id: $id}) "
        "RETURN n.name AS name, n.qualified_name AS qn, "
        "n.kind AS kind, n.file_path AS file_path LIMIT 1",
        {"p": project_id, "id": node_id},
    )
    if not node_rows:
        return None
    depth = max(1, min(depth, _MAX_DEPTH))
    limit = max(1, min(limit, _MAX_LIMIT))
    kind_list = resolve_kinds(kinds)
    callees, callees_total = await _neighbors(
        graph_store, project_id, node_id, "out", depth, limit, kind_list,
    )
    callers, callers_total = await _neighbors(
        graph_store, project_id, node_id, "in", depth, limit, kind_list,
    )
    node = node_rows[0]
    return {
        "node": {
            "id": node_id,
            "name": node["name"],
            "qualified_name": node["qn"],
            "kind": node["kind"],
            "file_path": node["file_path"],
        },
        "depth": depth,
        "kinds": kind_list,
        "callees": callees,
        "callees_total": callees_total,
        "callers": callers,
        "callers_total": callers_total,
    }
