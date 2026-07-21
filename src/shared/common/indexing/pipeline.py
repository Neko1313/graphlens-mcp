import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from graphlens import (
    GraphLens,
    LanguageAdapter,
    Node,
    Relation,
    adapter_registry,
)
from graphlens.models.graph import RESOLVER_STATUS_KEY

from shared.common.db.graph import GraphStore
from shared.common.db.vector import VectorStore
from shared.common.indexing import persist
from shared.common.indexing.embed import (
    EMBED_DIM,
    EMBED_KINDS,
    embed_text,
    encode,
)
from shared.common.indexing.result import IndexResult, ProgressCallback

__all__ = ["index_project_graph"]

_EMBED_BATCH = 256


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


def _under(file: Path, roots: list[Path]) -> bool:
    resolved = file.resolve()
    return any(resolved.is_relative_to(sub) for sub in roots)


def _collect_files(
    root: Path,
    adapter: LanguageAdapter,
    subpaths: list[str] | None,
) -> list[Path]:
    files = adapter.collect_files(root)
    if not subpaths:
        return files
    roots = [(root / sub).resolve() for sub in subpaths]
    return [f for f in files if _under(f, roots)]


def _count_files(
    root: Path,
    languages: list[tuple[str, LanguageAdapter]],
    subpaths: list[str] | None,
) -> int:
    return sum(
        len(_collect_files(root, adapter, subpaths))
        for _, adapter in languages
    )


async def _analyze(
    root: Path,
    languages: list[tuple[str, LanguageAdapter]],
    total: int,
    on_progress: ProgressCallback | None,
    subpaths: list[str] | None,
) -> tuple[GraphLens | None, dict[str, str]]:
    graph: GraphLens | None = None
    resolver_status: dict[str, str] = {}
    for lang, adapter in languages:
        await _report(on_progress, 0, total, f"Analyzing {lang}…")
        files = _collect_files(root, adapter, subpaths) if subpaths else None
        lang_graph = await asyncio.to_thread(adapter.analyze, root, files)
        resolver_status[lang] = str(
            lang_graph.metadata.get(RESOLVER_STATUS_KEY, "unknown"),
        )
        if graph is None:
            graph = lang_graph
        else:
            graph.merge(lang_graph, allow_shared=True)
    return graph, resolver_status


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


async def _persist_graph(
    root: Path,
    graph_store: GraphStore,
    project_id: str,
    nodes: list[Node],
    relations: list[Relation],
    total: int,
    on_progress: ProgressCallback | None,
) -> int:
    by_file = _group_by_file(root, nodes)
    n_files = max(len(by_file), 1)
    for index, (file_path, file_nodes) in enumerate(by_file.items(), start=1):
        await persist.persist_nodes(graph_store, root, project_id, file_nodes)
        await _report(
            on_progress, round(index / n_files * total), total,
            f"Indexed {file_path or '<no file>'}",
        )
    valid_ids = {node.id for node in nodes}
    return await persist.persist_relations(
        graph_store, project_id, relations, valid_ids,
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
                "id": node.id,
                "vector": vector,
                "project_id": project_id,
                "kind": node.kind.value,
                "name": node.name,
                "file_path": persist.norm_path(root, node.file_path) or "",
            }
            for node, vector in chunk
        ]
        await vector_store.upsert(project_id, rows)
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
    subpaths: list[str] | None = None,
) -> IndexResult:
    """Analyze a project with graphlens and persist the graph + embeddings.

    graphlens has no per-file parse hook, so progress is reported at the
    granularity we can honor: files discovered, per-language analysis,
    embedding prep, then per-file persistence. Writes are batched (one UNWIND
    per chunk) so real repos index in seconds. Analysis and embedding run
    before any destructive write, so a failure there leaves an existing index
    intact; re-indexing the same ``project_id`` is then idempotent.
    ``subpaths`` restricts indexing to those subdirectories (monorepo scope).
    """
    languages = await asyncio.to_thread(_discover, root)
    if not languages:
        msg = f"no indexable languages found under {root}"
        raise ValueError(msg)

    lang_names = [lang for lang, _ in languages]
    file_total = await asyncio.to_thread(
        _count_files, root, languages, subpaths,
    )
    total = max(file_total, 1)
    await _report(
        on_progress, 0, total,
        f"Found {file_total} files ({', '.join(lang_names)})",
    )

    graph, resolver_status = await _analyze(
        root, languages, total, on_progress, subpaths,
    )
    nodes = list(graph.nodes.values()) if graph is not None else []
    relations = graph.relations if graph is not None else []

    pairs = await _prepare_embeddings(root, nodes, total, on_progress)

    await persist.ensure_code_schema(graph_store)
    await vector_store.ensure_collection(project_id, EMBED_DIM)
    await persist.clear_project(graph_store, project_id)
    await vector_store.delete(project_id, f'project_id == "{project_id}"')

    rel_count = await _persist_graph(
        root, graph_store, project_id, nodes, relations, total, on_progress,
    )
    embedded = await _store_vectors(
        project_id, root, pairs, vector_store, total, on_progress,
    )

    await _report(on_progress, total, total, "Done")
    return IndexResult(
        languages=lang_names,
        files=file_total,
        nodes=len(nodes),
        relations=rel_count,
        embedded=embedded,
        resolver_status=resolver_status,
    )
