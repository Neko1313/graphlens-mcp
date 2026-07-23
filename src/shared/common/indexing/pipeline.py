import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

from graphlens import (
    GraphLens,
    LanguageAdapter,
    Node,
    adapter_registry,
)
from graphlens.metrics import RESOLVER_METRICS_KEY
from graphlens.models.graph import RESOLVER_STATUS_KEY

from entities.commit import CommitInfo
from shared.common.db.graph import GraphStore
from shared.common.db.vector import VectorStore
from shared.common.indexing import persist, temporal
from shared.common.indexing.embed import (
    EMBED_DIM,
    EMBED_KINDS,
    embed_text,
    encode,
)
from shared.common.indexing.result import IndexResult, ProgressCallback
from shared.common.setting.const import CODE_COLLECTION

__all__ = ["index_project_graph"]

_EMBED_BATCH = 256
_VECTOR_BATCH = 200


def _batched(items: list[Any], size: int) -> Iterator[list[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _group_by_file(root: Path, nodes: list[Node]) -> dict[str, list[Node]]:
    groups: dict[str, list[Node]] = {}
    for node in nodes:
        key = persist.norm_path(root, node.file_path) or ""
        groups.setdefault(key, []).append(node)
    return groups


async def _report(
    on_progress: ProgressCallback | None,
    done: float,
    total: float,
    message: str,
) -> None:
    if on_progress is not None:
        await on_progress(done, total, message)


def _discover(root: Path) -> list[tuple[str, LanguageAdapter]]:
    languages = []
    for lang in adapter_registry.available():
        adapter = adapter_registry.load(lang)()
        if adapter.can_handle(root):
            languages.append((lang, adapter))
    return languages


def _count_files(
    root: Path,
    languages: list[tuple[str, LanguageAdapter]],
) -> int:
    return sum(len(adapter.collect_files(root)) for _, adapter in languages)


def _unresolved_count(graph: GraphLens) -> int:
    metrics = graph.metadata.get(RESOLVER_METRICS_KEY)
    if not isinstance(metrics, dict):
        return 0
    raw = cast("dict[str, Any]", metrics).get("unresolved", 0)
    return int(raw) if isinstance(raw, (int, float)) else 0


async def _analyze(
    root: Path,
    languages: list[tuple[str, LanguageAdapter]],
    total: int,
    on_progress: ProgressCallback | None,
) -> tuple[GraphLens | None, dict[str, str], int]:
    graph: GraphLens | None = None
    resolver_status: dict[str, str] = {}
    # Summed per-language before merge: merge's metadata.update overwrites the
    # metrics key, so the last language would otherwise win.
    unresolved = 0
    for lang, adapter in languages:
        await _report(on_progress, 0, total, f"Analyzing {lang}…")
        lang_graph = await asyncio.to_thread(adapter.analyze, root, None)
        resolver_status[lang] = str(
            lang_graph.metadata.get(RESOLVER_STATUS_KEY, "unknown"),
        )
        unresolved += _unresolved_count(lang_graph)
        if graph is None:
            graph = lang_graph
        else:
            graph.merge(lang_graph, allow_shared=True)
    return graph, resolver_status, unresolved


def _encode_chunk(root: Path, chunk: list[Node]) -> list[list[float]]:
    vectors = encode([embed_text(root, node) for node in chunk])
    return [vector.tolist() for vector in vectors]


async def _prepare_embeddings(
    root: Path,
    nodes: list[Node],
    total: int,
    on_progress: ProgressCallback | None,
) -> list[tuple[Node, list[float]]]:
    """Encode embeddings BEFORE any store mutation (so a failure here never
    wipes an existing index) and off the event loop.
    """
    embeddable = [
        node
        for node in nodes
        if node.kind.value in EMBED_KINDS and node.file_path
    ]
    pairs: list[tuple[Node, list[float]]] = []
    for chunk in _batched(embeddable, _EMBED_BATCH):
        vectors = await asyncio.to_thread(_encode_chunk, root, chunk)
        pairs.extend(zip(chunk, vectors, strict=True))
        await _report(
            on_progress, 0, total,
            f"Prepared embeddings {len(pairs)}/{len(embeddable)}…",
        )
    return pairs


async def _persist_touched(
    root: Path,
    graph_store: GraphStore,
    project_id: str,
    nodes: list[Node],
    total: int,
    on_progress: ProgressCallback | None,
) -> None:
    """Upsert the added/changed nodes, reporting progress per file."""
    by_file = _group_by_file(root, nodes)
    n_files = max(len(by_file), 1)
    for index, (file_path, file_nodes) in enumerate(by_file.items(), start=1):
        await persist.persist_nodes(graph_store, root, project_id, file_nodes)
        await _report(
            on_progress, round(index / n_files * total), total,
            f"Indexed {file_path or '<no file>'}",
        )


async def _delete_vectors(
    vector_store: VectorStore,
    project_id: str,
    local_ids: list[str],
) -> None:
    """Delete vectors for removed nodes (a no-op for non-embeddable ones)."""
    gids = [persist.gid(project_id, lid) for lid in local_ids]
    for start in range(0, len(gids), _VECTOR_BATCH):
        chunk = gids[start : start + _VECTOR_BATCH]
        await vector_store.delete(
            CODE_COLLECTION, persist.vector_ids_filter(chunk),
        )


async def _store_vectors(
    project_id: str,
    root: Path,
    pairs: list[tuple[Node, list[float]]],
    vector_store: VectorStore,
    total: int,
    on_progress: ProgressCallback | None,
) -> int:
    embedded = 0
    for chunk in _batched(pairs, _EMBED_BATCH):
        rows = [
            {
                "id": persist.gid(project_id, node.id),
                "vector": vector,
                "project_id": project_id,
                "local_id": node.id,
                "kind": node.kind.value,
                "name": node.name,
                "file_path": persist.norm_path(root, node.file_path) or "",
            }
            for node, vector in chunk
        ]
        await vector_store.upsert(CODE_COLLECTION, rows)
        embedded += len(rows)
        await _report(
            on_progress, total, total, f"Stored {embedded} embeddings…",
        )
    return embedded


async def index_project_graph(
    root: Path,
    project_id: str,
    graph_store: GraphStore,
    vector_store: VectorStore,
    on_progress: ProgressCallback | None = None,
    commit: CommitInfo | None = None,
) -> IndexResult:
    """Analyze a project with graphlens and persist the graph + embeddings.

    Incremental by diff, not by wiping and rebuilding: graphlens can't resolve
    cross-file edges from a file subset, so the whole project is re-analyzed
    (which re-resolves every edge — the blast radius comes for free), but only
    the delta is written. Nodes whose ``content_hash`` is unchanged skip both
    the graph write and the (dominant-cost) re-embedding; added/changed nodes
    are upserted and re-embedded; vanished nodes and their vectors are deleted;
    edges are replaced wholesale (they carry no embedding cost). Analysis and
    embedding run before any destructive write, so a failure leaves the prior
    index intact. The whole repository under ``root`` is indexed. Graph nodes
    and vectors share one store/collection, scoped by the ``project_id``
    filter.
    """
    languages = await asyncio.to_thread(_discover, root)
    if not languages:
        msg = f"no indexable languages found under {root}"
        raise ValueError(msg)

    lang_names = [lang for lang, _ in languages]
    file_total = await asyncio.to_thread(_count_files, root, languages)
    total = max(file_total, 1)
    await _report(
        on_progress, 0, total,
        f"Found {file_total} files ({', '.join(lang_names)})",
    )

    graph, resolver_status, unresolved = await _analyze(
        root, languages, total, on_progress,
    )
    nodes = list(graph.nodes.values()) if graph is not None else []
    relations = graph.relations if graph is not None else []

    await persist.ensure_code_schema(graph_store)
    await vector_store.ensure_collection(CODE_COLLECTION, EMBED_DIM)

    # Diff the fresh graph against what's stored: touch only what changed.
    # Drop the file-line cache first so no stale entry survives across runs.
    persist.reset_source_cache()
    stored = await persist.stored_node_hashes(graph_store, project_id)
    new_by_id = {node.id: node for node in nodes}
    new_hash = {
        nid: persist.content_hash(root, node)
        for nid, node in new_by_id.items()
    }
    touched = [
        node
        for nid, node in new_by_id.items()
        if stored.get(nid) != new_hash[nid]
    ]
    removed = [lid for lid in stored if lid not in new_by_id]

    # Embed the touched nodes before any destructive write.
    pairs = await _prepare_embeddings(root, touched, total, on_progress)

    # Vectors first, graph second: the stored content_hash is the diff
    # baseline, so it must be the LAST thing written. A failure then leaves the
    # baseline stale and the next index re-touches (idempotent upsert/delete),
    # which self-heals — writing the hash first would strand the vector.
    if removed:
        await _delete_vectors(vector_store, project_id, removed)
    embedded = await _store_vectors(
        project_id, root, pairs, vector_store, total, on_progress,
    )

    await _persist_touched(
        root, graph_store, project_id, touched, total, on_progress,
    )
    if removed:
        await persist.delete_nodes(graph_store, project_id, removed)
    await persist.clear_relations(graph_store, project_id)
    rel_count = await persist.persist_relations(
        graph_store, project_id, relations, set(new_by_id),
    )

    # Record the graph head right after the graph body (and before temporal),
    # so it can only ever claim a sha the graph already reflects: a temporal
    # failure then can't leave graph_head lagging the body and mis-firing the
    # remote_head no-op.
    if commit is not None:
        await persist.set_graph_head(graph_store, project_id, commit.sha)

    embeddable_total = sum(
        1 for node in nodes
        if node.kind.value in EMBED_KINDS and node.file_path
    )

    changes = None
    if commit is not None:
        await temporal.ensure_temporal_schema(graph_store)
        changes = await temporal.append_versions(
            graph_store, root, project_id, commit, nodes, relations,
        )

    await _report(on_progress, total, total, "Done")
    return IndexResult(
        languages=lang_names,
        files=file_total,
        nodes=len(nodes),
        relations=rel_count,
        embedded=embedded,
        resolver_status=resolver_status,
        reused=embeddable_total - embedded,
        deleted=len(removed),
        unresolved=unresolved,
        ref=commit.ref if commit is not None else None,
        changes=changes,
    )
