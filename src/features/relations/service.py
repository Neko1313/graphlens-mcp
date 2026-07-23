from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from entities.result import NodeRef, RelationsResult, Revision
from shared.common.db.graph import GraphExecutor
from shared.common.indexing import temporal

__all__ = ["get_relations", "get_relations_at"]

_MAX_DEPTH = 5
_MAX_LIMIT = 200
# A hop's frontier ceiling: a hub symbol can fan out without bound, and a
# historical walk pays per hop in queries rather than in one engine traversal.
_MAX_FRONTIER = 2000
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
    graph_store: GraphExecutor,
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
    graph_store: GraphExecutor,
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


Group = tuple[list[NodeRef], int, int]
GroupFetch = Callable[[str, str, int], Awaitable[Group]]


async def _assemble(
    node: NodeRef,
    depth: int,
    kinds: str,
    fetch: GroupFetch,
) -> RelationsResult:
    """Fill the four navigation groups from a (direction, kind, depth) fetch.

    The grouping is the feature's contract and is identical live or
    historically; only where the neighbours come from differs.
    """
    wanted = _wanted_kinds(kinds)
    result = RelationsResult(node=node, depth=depth, kinds=sorted(wanted))
    if "calls" in wanted:
        result.callees, result.callees_total, result.callees_unresolved = (
            await fetch("out", "calls", depth)
        )
        result.callers, result.callers_total, _ = await fetch(
            "in", "calls", depth,
        )
    if "inherits_from" in wanted:
        result.implementors, result.implementors_total, _ = await fetch(
            "in", "inherits_from", 1,
        )
    if "references" in wanted:
        result.references, result.references_total, _ = await fetch(
            "in", "references", 1,
        )
    return result


async def get_relations(
    graph_store: GraphExecutor,
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

    async def fetch(direction: str, kind: str, hops: int) -> Group:
        return await _group(
            graph_store, project_id, node_id, direction, kind, hops, limit,
        )

    return await _assemble(node, depth, kinds, fetch)


def _ref_from_row(row: dict[str, Any]) -> NodeRef:
    return NodeRef(
        id=str(row["node_id"]),
        name=str(row["name"]),
        qualified_name=str(row["qualified_name"]),
        kind=str(row["kind"]),
        file_path=row["file_path"],
    )


@dataclass(frozen=True)
class _Hop:
    """One traversal shape: which way, over which edge kind, how far."""

    direction: str
    kind: str
    depth: int


async def _walk(
    graph_store: GraphExecutor,
    project_id: str,
    point: temporal.Point,
    start: str,
    hop: _Hop,
) -> set[str]:
    """Breadth-first over the edges recorded at ``point``, one query per hop.

    The live path hands the whole traversal to the engine; the log stores
    versions rather than edges, so the walk is driven here instead.
    """
    far = "target_id" if hop.direction == "out" else "source_id"
    seen: set[str] = set()
    frontier = [start]
    for _ in range(hop.depth):
        if not frontier:
            break
        rows = await temporal.edges_at(
            graph_store, project_id, point.ref, point.seq,
            frontier, hop.direction, hop.kind,
        )
        next_hop = []
        for row in rows:
            other = str(row[far])
            if other == start or other in seen:
                continue
            seen.add(other)
            next_hop.append(other)
        frontier = next_hop[:_MAX_FRONTIER]
    return seen


async def _group_at(
    graph_store: GraphExecutor,
    project_id: str,
    point: temporal.Point,
    node_id: str,
    hop: _Hop,
    limit: int,
) -> Group:
    """One navigation group as recorded at ``point``."""
    reached = await _walk(graph_store, project_id, point, node_id, hop)
    if not reached:
        return [], 0, 0
    rows = await temporal.state_at(
        graph_store, project_id, point.ref, point.seq, sorted(reached),
    )
    resolved = [r for r in rows if r["kind"] != _UNRESOLVED_KIND]
    unresolved = len(rows) - len(resolved)
    members = sorted(
        (_ref_from_row(row) for row in resolved),
        key=lambda ref: ref.qualified_name,
    )
    return members[:limit], len(members), unresolved


async def get_relations_at(
    graph_store: GraphExecutor,
    project_id: str,
    point: temporal.Point,
    node_id: str,
    depth: int = 2,
    limit: int = 25,
    kinds: str = "",
) -> RelationsResult | None:
    """The four navigation groups as they were at ``point`` in history.

    Answered entirely from the version log, so the neighbours are the ones
    recorded at that commit — a call that has since been removed is still
    there, and one added later is not.
    """
    rows = await temporal.state_at(
        graph_store, project_id, point.ref, point.seq, [node_id],
    )
    if not rows:
        return None
    depth = max(1, min(depth, _MAX_DEPTH))
    limit = max(1, min(limit, _MAX_LIMIT))

    async def fetch(direction: str, kind: str, hops: int) -> Group:
        return await _group_at(
            graph_store, project_id, point, node_id,
            _Hop(direction, kind, hops), limit,
        )

    result = await _assemble(_ref_from_row(rows[0]), depth, kinds, fetch)
    result.revision = Revision(
        ref=point.ref,
        seq=point.seq,
        sha=point.sha,
        time_update=point.time_update,
        is_head=point.is_head,
    )
    return result
