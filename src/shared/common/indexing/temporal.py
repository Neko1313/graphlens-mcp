import json
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from graphlens import Node

from entities.commit import CommitInfo
from shared.common.db.graph import GraphExecutor, GraphStore
from shared.common.indexing.persist import content_hash, norm_path

__all__ = [
    "append_versions",
    "clear_project",
    "ensure_temporal_schema",
    "state_at",
]

_BATCH = 500
_DELETE = "delete"


def _chunks(rows: list[dict], size: int) -> Iterator[list[dict]]:
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


def _version_id(project_id: str, ref: str, node_id: str, seq: int) -> str:
    """PK of one append-only version row — unique per (node, ref, seq)."""
    return f"{project_id}::{ref}::{node_id}::{seq}"


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
        "PRIMARY KEY(id))",
    )


async def clear_project(store: GraphStore, project_id: str) -> None:
    """Drop a project's version history, per-ref heads, and commit map.

    One transaction: a half-cleared log — say heads gone but versions kept —
    would let the next index restart the seq counter over rows that already
    exist at those seqs.
    """
    await ensure_temporal_schema(store)
    async with store.transaction() as tx:
        for label in ("NodeVersion", "RefState", "RefCommit"):
            await tx.execute(
                f"MATCH (v:{label} {{project_id: $p}}) DELETE v",
                {"p": project_id},
            )


async def state_at(
    store: GraphExecutor,
    project_id: str,
    ref: str,
    at_seq: int,
) -> list[dict[str, Any]]:
    """The live node set of ``ref`` as of ``at_seq`` (inclusive).

    Gates the log to ``seq <= at_seq`` *first*, then takes the greatest-seq row
    per node (a flat WHERE would lose history) and drops tombstones. Expressed
    as a max-then-rematch aggregation rather than a window function, since Kuzu
    has no ``ROW_NUMBER() OVER`` — and the pattern is verified on the engine in
    tests.
    """
    return await store.execute(
        "MATCH (v:NodeVersion {project_id: $p, ref: $ref}) "
        "WHERE v.seq <= $at "
        "WITH v.node_id AS nid, max(v.seq) AS mx "
        "MATCH (w:NodeVersion {project_id: $p, ref: $ref, node_id: nid, "
        "seq: mx}) "
        "WHERE w.status <> $deleted "
        "RETURN w.node_id AS node_id, w.qualified_name AS qualified_name, "
        "w.name AS name, w.kind AS kind, w.file_path AS file_path, "
        "w.span AS span, w.content_hash AS content_hash, "
        "w.seq AS seq, w.time_update AS time_update",
        {"p": project_id, "ref": ref, "at": at_seq, "deleted": _DELETE},
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
        "SET c.project_id = $p, c.ref = $ref, c.sha = $sha, c.seq = $seq",
        {"id": f"{project_id}::{commit.ref}::{commit.sha}", **params},
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
) -> dict[str, int]:
    """Append this commit's node changes to the ref's log, idempotently.

    ``seq`` is assigned here, not by git: a commit already in the log resolves
    to its recorded seq (re-indexing it, from any clone shallow or full, is a
    no-op), and a new commit takes ``head_seq + 1``. So the head only ever
    advances and the seq is clone-independent. The incoming node set is diffed
    against the ref's live state at the current head: absent-before nodes are
    ``create``, content-changed (by ``content_hash``) are ``update``, vanished
    nodes get a ``delete`` tombstone, unchanged nodes are skipped. Returns the
    change counts plus the ``seq`` this commit landed at.

    The whole append — the baseline read, the version rows, and the two head
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

        rows, counts = _node_rows(
            root, project_id, commit, seq, prev,
            {node.id: node for node in nodes},
        )
        await _write_node_versions(tx, project_id, ref, seq, rows)
        await _advance_head(tx, project_id, commit, seq)

    counts["seq"] = seq
    return counts
