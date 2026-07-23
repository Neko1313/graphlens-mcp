import hashlib
import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from graphlens import Node, Relation

from entities.commit import CommitInfo
from shared.common.db.graph import GraphExecutor, GraphStore
from shared.common.indexing.persist import content_hash, graph_head, norm_path

__all__ = [
    "Point",
    "append_versions",
    "clear_project",
    "edges_at",
    "ensure_temporal_schema",
    "list_commits",
    "list_refs",
    "resolve_point",
    "state_at",
]

_BATCH = 500
_DELETE = "delete"
# One IN-list per query: keeps a big frontier off a single statement.
_ID_CHUNK = 500


@dataclass(frozen=True)
class Point:
    """One resolved point in a project's history — what a read is answered at.

    ``seq`` is the log's own monotonic counter for the ref (index order, not
    git topology); ``sha`` is the commit indexed at that seq.
    """

    ref: str
    seq: int
    sha: str | None = None
    time_update: int | None = None
    is_head: bool = False


def _chunks(rows: list[dict], size: int) -> Iterator[list[dict]]:
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


def _id_chunks(ids: list[str]) -> Iterator[list[str]]:
    for start in range(0, len(ids), _ID_CHUNK):
        yield ids[start : start + _ID_CHUNK]


def _version_id(project_id: str, ref: str, node_id: str, seq: int) -> str:
    """PK of one append-only version row — unique per (node, ref, seq)."""
    return f"{project_id}::{ref}::{node_id}::{seq}"


def _edge_id(source_id: str, kind: str, target_id: str) -> str:
    """An edge's identity: the graph stores one edge per (from, kind, to)."""
    return f"{source_id}>{kind}>{target_id}"


def _span_repr(node: Node) -> str:
    span = node.span
    if span is None:
        return ""
    return f"{span.start_line},{span.start_col},{span.end_line},{span.end_col}"


async def ensure_temporal_schema(store: GraphExecutor) -> None:
    """Create the append-only version log and per-ref head tables."""
    await store.execute(
        "CREATE NODE TABLE IF NOT EXISTS NodeVersion("
        "id STRING, project_id STRING, ref STRING, node_id STRING, "
        "seq INT64, time_update INT64, status STRING, content_hash STRING, "
        "qualified_name STRING, name STRING, kind STRING, "
        "file_path STRING, span STRING, metadata STRING, "
        "PRIMARY KEY(id))",
    )
    await store.execute(
        "CREATE NODE TABLE IF NOT EXISTS RefState("
        "id STRING, project_id STRING, ref STRING, head_sha STRING, "
        "head_seq INT64, parent_ref STRING, branch_point_seq INT64, "
        "PRIMARY KEY(id))",
    )
    # Maps each indexed commit sha to the seq it was assigned, so the seq is
    # clone-independent (a shallow and a full clone of one commit resolve to
    # the same seq) and re-indexing a known commit is a cheap no-op.
    await store.execute(
        "CREATE NODE TABLE IF NOT EXISTS RefCommit("
        "id STRING, project_id STRING, ref STRING, sha STRING, seq INT64, "
        "time_update INT64, PRIMARY KEY(id))",
    )
    # Edges get the same append-only treatment as nodes: without it a past
    # point-in-time would carry today's edges, which is precisely the answer
    # (who called what, then) a history query is asking for.
    await store.execute(
        "CREATE NODE TABLE IF NOT EXISTS RelVersion("
        "id STRING, project_id STRING, ref STRING, edge_id STRING, "
        "source_id STRING, target_id STRING, kind STRING, "
        "seq INT64, time_update INT64, status STRING, content_hash STRING, "
        "metadata STRING, PRIMARY KEY(id))",
    )


async def clear_project(store: GraphStore, project_id: str) -> None:
    """Drop a project's version history, per-ref heads, and commit map.

    One transaction: a half-cleared log — say heads gone but versions kept —
    would let the next index restart the seq counter over rows that already
    exist at those seqs.
    """
    await ensure_temporal_schema(store)
    async with store.transaction() as tx:
        for label in ("NodeVersion", "RelVersion", "RefState", "RefCommit"):
            await tx.execute(
                f"MATCH (v:{label} {{project_id: $p}}) DELETE v",
                {"p": project_id},
            )


_NODE_FIELDS = (
    "w.node_id AS node_id, w.qualified_name AS qualified_name, "
    "w.name AS name, w.kind AS kind, w.file_path AS file_path, "
    "w.span AS span, w.metadata AS metadata, "
    "w.content_hash AS content_hash, w.seq AS seq, "
    "w.time_update AS time_update"
)


async def state_at(
    store: GraphExecutor,
    project_id: str,
    ref: str,
    at_seq: int,
    node_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """The live node set of ``ref`` as of ``at_seq`` (inclusive).

    Gates the log to ``seq <= at_seq`` *first*, then takes the greatest-seq row
    per node (a flat WHERE would lose history) and drops tombstones. Expressed
    as a max-then-rematch aggregation rather than a window function, since Kuzu
    has no ``ROW_NUMBER() OVER`` — and the pattern is verified on the engine in
    tests. ``node_ids`` narrows it to specific nodes, which is what navigation
    needs: resolving a frontier costs one query, not a whole-project scan.
    """
    params: dict[str, Any] = {
        "p": project_id, "ref": ref, "at": at_seq, "deleted": _DELETE,
    }
    if node_ids is None:
        return await _state_query(store, params, "")
    rows: list[dict[str, Any]] = []
    for chunk in _id_chunks(node_ids):
        rows.extend(
            await _state_query(store, {**params, "ids": chunk}, "v.node_id"),
        )
    return rows


async def _state_query(
    store: GraphExecutor,
    params: dict[str, Any],
    in_field: str,
) -> list[dict[str, Any]]:
    # The IN predicate belongs to the gating MATCH, before the aggregation:
    # applying it after would still be correct but would rank the whole log.
    narrow = f" AND {in_field} IN $ids" if in_field else ""
    return await store.execute(
        "MATCH (v:NodeVersion {project_id: $p, ref: $ref}) "
        f"WHERE v.seq <= $at{narrow} "
        "WITH v.node_id AS nid, max(v.seq) AS mx "
        "MATCH (w:NodeVersion {project_id: $p, ref: $ref, node_id: nid, "
        "seq: mx}) "
        f"WHERE w.status <> $deleted RETURN {_NODE_FIELDS}",
        params,
    )


_EDGE_FIELDS = (
    "w.edge_id AS edge_id, w.source_id AS source_id, "
    "w.target_id AS target_id, w.kind AS kind, "
    "w.content_hash AS content_hash, w.metadata AS metadata"
)


async def _edge_query(
    store: GraphExecutor,
    params: dict[str, Any],
    predicate: str,
) -> list[dict[str, Any]]:
    return await store.execute(
        "MATCH (v:RelVersion {project_id: $p, ref: $ref}) "
        f"WHERE v.seq <= $at{predicate} "
        "WITH v.edge_id AS eid, max(v.seq) AS mx "
        "MATCH (w:RelVersion {project_id: $p, ref: $ref, edge_id: eid, "
        "seq: mx}) "
        f"WHERE w.status <> $deleted RETURN {_EDGE_FIELDS}",
        params,
    )


async def edges_at(
    store: GraphExecutor,
    project_id: str,
    ref: str,
    at_seq: int,
    node_ids: list[str],
    direction: str,
    kind: str,
) -> list[dict[str, Any]]:
    """Live ``kind`` edges at ``at_seq`` leaving (``out``) or entering
    (``in``) any of ``node_ids`` — one hop of a historical traversal.

    Tombstones carry their endpoints and kind precisely so they survive this
    filter and can mask the create they supersede; without that, a deleted
    edge would reappear at every later point in time.
    """
    side = "source_id" if direction == "out" else "target_id"
    params: dict[str, Any] = {
        "p": project_id, "ref": ref, "at": at_seq,
        "deleted": _DELETE, "kind": kind,
    }
    rows: list[dict[str, Any]] = []
    for chunk in _id_chunks(node_ids):
        rows.extend(
            await _edge_query(
                store,
                {**params, "ids": chunk},
                f" AND v.kind = $kind AND v.{side} IN $ids",
            ),
        )
    return rows


async def _live_edges(
    store: GraphExecutor,
    project_id: str,
    ref: str,
    at_seq: int,
) -> dict[str, dict[str, Any]]:
    """Every live edge of ``ref`` at ``at_seq``, keyed by edge id — the
    baseline the incoming edge set is diffed against.
    """
    rows = await _edge_query(
        store,
        {"p": project_id, "ref": ref, "at": at_seq, "deleted": _DELETE},
        "",
    )
    return {row["edge_id"]: row for row in rows}


async def list_refs(
    store: GraphExecutor,
    project_id: str,
) -> list[dict[str, Any]]:
    """Every ref the project has been indexed on, with its head sha and seq."""
    return await store.execute(
        "MATCH (s:RefState {project_id: $p}) "
        "RETURN s.ref AS ref, s.head_sha AS head_sha, s.head_seq AS head_seq "
        "ORDER BY s.ref",
        {"p": project_id},
    )


async def list_commits(
    store: GraphExecutor,
    project_id: str,
    ref: str,
) -> list[dict[str, Any]]:
    """The commits indexed on ``ref``, newest first — the valid ``at``
    values.
    """
    return await store.execute(
        "MATCH (c:RefCommit {project_id: $p, ref: $ref}) "
        "RETURN c.sha AS sha, c.seq AS seq, c.time_update AS time_update "
        "ORDER BY c.seq DESC",
        {"p": project_id, "ref": ref},
    )


async def _default_ref(
    store: GraphExecutor,
    project_id: str,
    refs: list[dict[str, Any]],
) -> dict[str, Any]:
    """The ref to read when the caller named none.

    Prefer whichever ref the live graph currently reflects, so an unqualified
    history read lines up with what the ordinary tools return.
    """
    if len(refs) == 1:
        return refs[0]
    head = await graph_head(store, project_id)
    return next((r for r in refs if r["head_sha"] == head), refs[0])


def _pick_commit(
    commits: list[dict[str, Any]],
    at: str,
) -> dict[str, Any] | None:
    """Resolve ``at`` — a seq number, or a full/prefix commit sha.

    The two overlap: a sha is hex, so a prefix can be all digits. Seq wins when
    one matches (they are small counting numbers, and a caller who passes 3
    means the third indexed commit), and an all-digit value that matches no seq
    falls through to the sha reading rather than resolving to nothing.
    """
    if at.isdigit():
        wanted = int(at)
        by_seq = next((c for c in commits if int(c["seq"]) == wanted), None)
        if by_seq is not None:
            return by_seq
    matches = [c for c in commits if str(c["sha"]).startswith(at)]
    return matches[0] if len(matches) == 1 else None


async def resolve_point(
    store: GraphExecutor,
    project_id: str,
    ref: str = "",
    at: str = "",
) -> Point | None:
    """Resolve ``(ref, at)`` to a point in the log, or ``None`` if unknown.

    ``at`` is a commit sha (full or an unambiguous prefix) or a raw seq;
    empty means the ref's head. An ambiguous sha prefix resolves to nothing
    rather than to an arbitrary commit.
    """
    refs = await list_refs(store, project_id)
    if not refs:
        return None
    if ref:
        chosen = next((r for r in refs if r["ref"] == ref), None)
        if chosen is None:
            return None
    else:
        chosen = await _default_ref(store, project_id, refs)
    name, head_seq = str(chosen["ref"]), int(chosen["head_seq"])
    commits = await list_commits(store, project_id, name)
    if not at:
        head = next((c for c in commits if int(c["seq"]) == head_seq), None)
        return Point(
            ref=name,
            seq=head_seq,
            sha=str(chosen["head_sha"]) if chosen["head_sha"] else None,
            time_update=int(head["time_update"]) if head else None,
            is_head=True,
        )
    commit = _pick_commit(commits, at)
    if commit is None:
        return None
    seq = int(commit["seq"])
    return Point(
        ref=name,
        seq=seq,
        sha=str(commit["sha"]),
        time_update=int(commit["time_update"]),
        is_head=seq == head_seq,
    )


def _row(
    project_id: str,
    commit: CommitInfo,
    seq: int,
    node_id: str,
    status: str,
    node: Node | None,
    root: Path,
) -> dict[str, Any]:
    span = None
    content = ""
    if node is not None:
        content = content_hash(root, node)
        if node.span is not None:
            span = _span_repr(node)
    return {
        "id": _version_id(project_id, commit.ref, node_id, seq),
        "project_id": project_id,
        "ref": commit.ref,
        "node_id": node_id,
        "seq": seq,
        "time_update": commit.time_update,
        "status": status,
        "content_hash": content,
        "qn": node.qualified_name if node is not None else "",
        "name": node.name if node is not None else "",
        "kind": node.kind.value if node is not None else "",
        "fp": norm_path(root, node.file_path) if node is not None else None,
        "span": span,
        "meta": json.dumps(node.metadata, default=str) if node else "{}",
    }


async def _ref_state(
    store: GraphExecutor,
    project_id: str,
    ref: str,
) -> tuple[str | None, int]:
    """The ref's recorded ``(head_sha, head_seq)`` — ``(None, 0)`` if new."""
    rows = await store.execute(
        "MATCH (s:RefState {id: $id}) "
        "RETURN s.head_sha AS sha, s.head_seq AS seq",
        {"id": f"{project_id}::{ref}"},
    )
    if not rows:
        return None, 0
    return rows[0]["sha"], int(rows[0]["seq"])


async def _commit_seq(
    store: GraphExecutor,
    project_id: str,
    ref: str,
    sha: str,
) -> int | None:
    """The seq a commit was already indexed at on this ref, or ``None``."""
    rows = await store.execute(
        "MATCH (c:RefCommit {id: $id}) RETURN c.seq AS seq",
        {"id": f"{project_id}::{ref}::{sha}"},
    )
    return int(rows[0]["seq"]) if rows else None


def _node_rows(
    root: Path,
    project_id: str,
    commit: CommitInfo,
    seq: int,
    prev: dict[str, str],
    nodes: dict[str, Node],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Diff the incoming node set against ``prev`` into version rows."""
    counts = {"created": 0, "updated": 0, "deleted": 0, "unchanged": 0}
    rows: list[dict[str, Any]] = []
    for node_id, node in nodes.items():
        digest = content_hash(root, node)
        if node_id not in prev:
            status = "create"
            counts["created"] += 1
        elif prev[node_id] != digest:
            status = "update"
            counts["updated"] += 1
        else:
            counts["unchanged"] += 1
            continue
        rows.append(_row(project_id, commit, seq, node_id, status, node, root))
    for node_id in prev.keys() - nodes.keys():
        counts["deleted"] += 1
        rows.append(
            _row(project_id, commit, seq, node_id, _DELETE, None, root),
        )
    return rows, counts


async def _write_node_versions(
    tx: GraphExecutor,
    project_id: str,
    ref: str,
    seq: int,
    rows: list[dict[str, Any]],
) -> None:
    # Clearing the slot first is belt-and-braces: within this transaction it
    # is a no-op, but it also sweeps rows stranded at this seq by a store
    # written before the append became transactional.
    await tx.execute(
        "MATCH (v:NodeVersion {project_id: $p, ref: $ref, seq: $seq}) "
        "DELETE v",
        {"p": project_id, "ref": ref, "seq": seq},
    )
    for chunk in _chunks(rows, _BATCH):
        await tx.execute(
            "UNWIND $rows AS r MERGE (v:NodeVersion {id: r.id}) "
            "SET v.project_id = r.project_id, v.ref = r.ref, "
            "v.node_id = r.node_id, v.seq = r.seq, "
            "v.time_update = r.time_update, v.status = r.status, "
            "v.content_hash = r.content_hash, v.qualified_name = r.qn, "
            "v.name = r.name, v.kind = r.kind, v.file_path = r.fp, "
            "v.span = r.span, v.metadata = r.meta",
            {"rows": chunk},
        )


def _edge_map(
    relations: Iterable[Relation],
    valid: set[str],
) -> dict[str, dict[str, Any]]:
    """The incoming edge set, deduplicated the way the graph stores it.

    Mirrors ``persist.persist_relations``: endpoints must both be present, and
    one row survives per (source, kind, target) — so the log and the live
    graph never disagree about what an edge is.
    """
    edges: dict[str, dict[str, Any]] = {}
    for relation in relations:
        if relation.source_id not in valid or relation.target_id not in valid:
            continue
        kind = relation.kind.value
        edge_id = _edge_id(relation.source_id, kind, relation.target_id)
        if edge_id in edges:
            continue
        meta = json.dumps(relation.metadata, sort_keys=True, default=str)
        edges[edge_id] = {
            "edge_id": edge_id,
            "source_id": relation.source_id,
            "target_id": relation.target_id,
            "kind": kind,
            "metadata": meta,
            "content_hash": hashlib.sha256(meta.encode()).hexdigest()[:16],
        }
    return edges


def _rel_row(
    project_id: str,
    commit: CommitInfo,
    seq: int,
    edge: dict[str, Any],
    status: str,
) -> dict[str, Any]:
    return {
        "id": _version_id(project_id, commit.ref, edge["edge_id"], seq),
        "project_id": project_id,
        "ref": commit.ref,
        "edge_id": edge["edge_id"],
        "source_id": edge["source_id"],
        "target_id": edge["target_id"],
        "kind": edge["kind"],
        "seq": seq,
        "time_update": commit.time_update,
        "status": status,
        "content_hash": edge["content_hash"] if status != _DELETE else "",
        "meta": edge["metadata"] if status != _DELETE else "{}",
    }


def _rel_rows(
    project_id: str,
    commit: CommitInfo,
    seq: int,
    prev: dict[str, dict[str, Any]],
    edges: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Diff the incoming edge set against ``prev`` into version rows."""
    counts = {"created": 0, "updated": 0, "deleted": 0}
    rows: list[dict[str, Any]] = []
    for edge_id, edge in edges.items():
        before = prev.get(edge_id)
        if before is None:
            status = "create"
            counts["created"] += 1
        elif before["content_hash"] != edge["content_hash"]:
            status = "update"
            counts["updated"] += 1
        else:
            continue
        rows.append(_rel_row(project_id, commit, seq, edge, status))
    for edge_id in prev.keys() - edges.keys():
        counts["deleted"] += 1
        rows.append(
            _rel_row(project_id, commit, seq, prev[edge_id], _DELETE),
        )
    return rows, counts


async def _write_rel_versions(
    tx: GraphExecutor,
    project_id: str,
    ref: str,
    seq: int,
    rows: list[dict[str, Any]],
) -> None:
    await tx.execute(
        "MATCH (v:RelVersion {project_id: $p, ref: $ref, seq: $seq}) DELETE v",
        {"p": project_id, "ref": ref, "seq": seq},
    )
    for chunk in _chunks(rows, _BATCH):
        await tx.execute(
            "UNWIND $rows AS r MERGE (v:RelVersion {id: r.id}) "
            "SET v.project_id = r.project_id, v.ref = r.ref, "
            "v.edge_id = r.edge_id, v.source_id = r.source_id, "
            "v.target_id = r.target_id, v.kind = r.kind, v.seq = r.seq, "
            "v.time_update = r.time_update, v.status = r.status, "
            "v.content_hash = r.content_hash, v.metadata = r.meta",
            {"rows": chunk},
        )


async def _advance_head(
    tx: GraphExecutor,
    project_id: str,
    commit: CommitInfo,
    seq: int,
) -> None:
    params = {
        "p": project_id, "ref": commit.ref, "sha": commit.sha, "seq": seq,
    }
    await tx.execute(
        "MERGE (c:RefCommit {id: $id}) "
        "SET c.project_id = $p, c.ref = $ref, c.sha = $sha, c.seq = $seq, "
        "c.time_update = $time_update",
        {
            "id": f"{project_id}::{commit.ref}::{commit.sha}",
            "time_update": commit.time_update,
            **params,
        },
    )
    await tx.execute(
        "MERGE (s:RefState {id: $id}) "
        "SET s.project_id = $p, s.ref = $ref, s.head_sha = $sha, "
        "s.head_seq = $seq",
        {"id": f"{project_id}::{commit.ref}", **params},
    )


async def append_versions(
    store: GraphStore,
    root: Path,
    project_id: str,
    commit: CommitInfo,
    nodes: Iterable[Node],
    relations: Iterable[Relation] = (),
) -> dict[str, int]:
    """Append this commit's node and edge changes to the ref's log.

    ``seq`` is assigned here, not by git: a commit already in the log resolves
    to its recorded seq (re-indexing it, from any clone shallow or full, is a
    no-op), and a new commit takes ``head_seq + 1``. So the head only ever
    advances and the seq is clone-independent. Both sets are diffed against the
    ref's live state at the current head: absent-before entries are ``create``,
    content-changed ones are ``update``, vanished ones get a ``delete``
    tombstone, unchanged ones are skipped. Returns the change counts plus the
    ``seq`` this commit landed at; edge counts are reported under ``edge_*``.

    Nodes and edges are versioned together because a point in time is only
    answerable with both: a node set without its edges of the day says what
    existed but not what called what.

    The whole append — the baseline reads, the version rows, and the two head
    records — runs in ONE transaction. Partially applied, it would be worse
    than not applied at all: rows at a seq whose head never advanced are
    invisible to every read yet block the next commit from using that seq.
    """
    ref = commit.ref
    existing = await _commit_seq(store, project_id, ref, commit.sha)
    if existing is not None:
        live = await state_at(store, project_id, ref, existing)
        return {
            "created": 0, "updated": 0, "deleted": 0,
            "unchanged": len(live), "seq": existing,
        }

    async with store.transaction() as tx:
        _head_sha, head_seq = await _ref_state(tx, project_id, ref)
        seq = head_seq + 1
        prev_rows = await state_at(tx, project_id, ref, head_seq)
        prev = {row["node_id"]: row["content_hash"] for row in prev_rows}
        by_id = {node.id: node for node in nodes}

        rows, counts = _node_rows(
            root, project_id, commit, seq, prev, by_id,
        )
        await _write_node_versions(tx, project_id, ref, seq, rows)

        prev_edges = await _live_edges(tx, project_id, ref, head_seq)
        edge_rows, edge_counts = _rel_rows(
            project_id, commit, seq, prev_edges,
            _edge_map(relations, set(by_id)),
        )
        await _write_rel_versions(tx, project_id, ref, seq, edge_rows)
        await _advance_head(tx, project_id, commit, seq)

    counts["seq"] = seq
    counts.update({f"edge_{key}": n for key, n in edge_counts.items()})
    return counts
