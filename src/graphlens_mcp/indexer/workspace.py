"""Workspace: orchestrates indexing, freshness checks, and cross-language linking."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import logging
import os
from pathlib import Path
from typing import Any

from graphlens import (
    RESOLVER_STATUS_KEY,
    GraphLens,
    LanguageAdapter,
    RelationKind,
    ResolverStatus,
    adapter_registry,
)

from graphlens_mcp.indexer.concurrency import MAX_CONCURRENT_RESOLVERS, InFlightRegistry
from graphlens_mcp.indexer.resolvers import (
    doctor,
    get_adapter,
    get_null_adapter,
)
from graphlens_mcp.store.sqlite_store import SqliteStore

# Built lazily on first use from adapter.file_extensions()
_EXT_TO_LANG: dict[str, str] | None = None


def _get_ext_map() -> dict[str, str]:
    global _EXT_TO_LANG  # noqa: PLW0603 — process-wide lazy cache of adapter extensions
    if _EXT_TO_LANG is None:
        mapping: dict[str, str] = {}
        for lang in adapter_registry.available():
            try:
                cls = adapter_registry.load(lang)
                for ext in cls().file_extensions():
                    mapping.setdefault(ext, lang)
            except Exception as exc:
                logger.debug("Skipping extension probe for %s: %s", lang, exc)
        _EXT_TO_LANG = mapping
    return _EXT_TO_LANG


logger = logging.getLogger(__name__)

_GRAPHLENS_DIR = ".graphlens"
_DB_NAME = "graph.db"

# Above this exposer-by-consumer product a single boundary stops synthesizing pairwise
# COMMUNICATES_WITH edges (a hub topic would otherwise blow up quadratically).
_MAX_BOUNDARY_FANOUT = 2000


def default_db_path(project_root: Path) -> Path:
    """Return the default graph database location for *project_root*."""
    return project_root / _GRAPHLENS_DIR / _DB_NAME


class Workspace:
    """Manages the indexing lifecycle for a project root."""

    def __init__(self, store: SqliteStore, project_root: Path) -> None:
        self.store = store
        self.project_root = project_root
        self._in_flight = InFlightRegistry()
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_RESOLVERS)
        # One long-lived adapter per language for the Workspace lifetime, so the
        # expensive adapter/resolver init (TS install cache, resolver objects) is
        # reused across the full index and every incremental re-index, instead of
        # being rebuilt on each call. (Invariant: resolver off the request hot path.)
        self._adapters: dict[str, LanguageAdapter | None] = {}
        self._null_adapters: dict[str, LanguageAdapter | None] = {}

    @classmethod
    async def create(cls, project_root: Path, db_path: Path) -> Workspace:
        """Open the store at *db_path* and return a Workspace for *project_root*."""
        store = await SqliteStore.create(db_path)
        return cls(store, project_root)

    def _adapter(self, lang: str) -> LanguageAdapter | None:
        """Return the pooled real adapter for *lang*, creating it once."""
        if lang not in self._adapters:
            self._adapters[lang] = get_adapter(lang)
        return self._adapters[lang]

    def _null_adapter(self, lang: str) -> LanguageAdapter | None:
        """Return the pooled skeleton (NullResolver) adapter for *lang*."""
        if lang not in self._null_adapters:
            self._null_adapters[lang] = get_null_adapter(lang)
        return self._null_adapters[lang]

    async def close(self) -> None:
        """Shut down pooled resolvers (e.g. ty LSP processes) and the store.

        Best-effort: prefer a public ``close``/``shutdown``/``stop`` on the adapter
        itself; fall back to its private ``_resolver`` only when the adapter exposes no
        public lifecycle hook. Idempotent — safe to call more than once.
        """
        for adapter in self._adapters.values():
            if adapter is None:
                continue
            self._shutdown_one(adapter)
        self._adapters.clear()
        self._null_adapters.clear()
        await self.store.close()

    @staticmethod
    def _shutdown_one(adapter: LanguageAdapter) -> None:
        # Try the adapter's own lifecycle hook first (public API), then the resolver's.
        for target in (adapter, getattr(adapter, "_resolver", None)):
            if target is None:
                continue
            for method in ("shutdown", "close", "stop"):
                fn = getattr(target, method, None)
                if callable(fn):
                    try:
                        fn()
                    except Exception as exc:  # pragma: no cover - best-effort cleanup
                        logger.debug("Shutdown via %s.%s failed: %s", target, method, exc)
                    return

    # ------------------------------------------------------------------
    # Full index
    # ------------------------------------------------------------------

    async def full_index(self) -> dict[str, Any]:
        """Full re-index of the project. Returns stats dict."""
        languages = adapter_registry.available()
        report = doctor(self.project_root)

        stats: dict[str, Any] = {
            "languages": {},
            "nodes": 0,
            "edges": 0,
            "files": 0,
        }

        merged_graph = GraphLens()

        # Analyze every applicable language concurrently — each analyze is a blocking
        # call run in the executor and gated by the resolver semaphore. Persisting and
        # merging stay sequential afterwards (writes serialize on the store's write lock
        # and merge() mutates shared in-memory state), so only the slow parse/resolve
        # phase is parallelized.
        targets = [
            lang
            for lang in languages
            if report.get(lang) is not None
            and (a := self._adapter(lang)) is not None
            and a.can_handle(self.project_root)
        ]
        graphs = await asyncio.gather(*(self._analyze_language(lang) for lang in targets))

        for lang, graph in zip(targets, graphs, strict=True):
            if graph is None:
                continue
            resolver_status = ResolverStatus.from_value(
                graph.metadata.get(RESOLVER_STATUS_KEY, "ok")
            )
            file_status = _resolver_to_file_status(resolver_status)

            await _persist_graph(self.store, graph, lang, file_status)

            try:
                merged_graph.merge(graph, allow_shared=True)
            except Exception as exc:
                logger.warning("Merge error for %s: %s", lang, exc)

            stats["languages"][lang] = {
                "status": resolver_status.value,
                "hint": report[lang].get("hint"),
            }

        # Persist fileless structural nodes/edges (project/module hierarchy,
        # boundary nodes) that the per-file ownership filter would otherwise drop.
        await self.store.apply_structural(merged_graph)

        # Synthesize COMMUNICATES_WITH after merging all language graphs
        cl_edges = _synthesize_cross_language_edges(merged_graph)
        if cl_edges:
            await self.store.apply_cross_language_edges(cl_edges)

        stats["nodes"] = await self.store.node_count()
        stats["edges"] = await self.store.edge_count()
        stats["files"] = await self.store.file_count()
        return stats

    async def _analyze_language(self, lang: str) -> GraphLens | None:
        """Run a full analyze for *lang* in the executor, gated by the semaphore."""
        adapter = self._adapter(lang)
        if adapter is None:
            return None
        logger.info("Indexing %s…", lang)
        try:
            async with self._semaphore:
                graph = await asyncio.get_running_loop().run_in_executor(
                    None, lambda a=adapter: a.analyze(self.project_root)
                )
        except Exception as exc:
            logger.warning("Analyze failed for %s: %s", lang, exc)
            return None
        return _normalize_graph_paths(graph, self.project_root)

    # ------------------------------------------------------------------
    # On-access freshness
    # ------------------------------------------------------------------

    async def ensure_fresh(self, file_path: Path, *, semantic: bool = False) -> str:
        """Ensure file_path is up-to-date in the store and report its graph status.

        Returns 'ok', 'skeleton', or 'degraded'. The file itself is re-indexed on
        access when its bytes changed. For semantic queries the status is additionally
        downgraded to 'degraded' when one of the file's *imported dependencies* changed
        on disk: single-file analysis cannot re-resolve cross-file calls into the new
        definition, so the stored edges may be stale until a full ``reindex`` — and the
        agent should be told, not handed an 'ok' it cannot trust (transitive freshness).
        """
        path_str = str(file_path.resolve())
        base = await self._ensure_fresh_self(file_path, path_str, semantic=semantic)
        if semantic and base in ("ok", "degraded") and await self._dependency_stale(path_str):
            return "degraded"
        return base

    async def _ensure_fresh_self(self, file_path: Path, path_str: str, *, semantic: bool) -> str:
        """Freshness of *file_path* itself (no transitive dependency check)."""
        try:
            stat = Path(path_str).stat()
        except OSError:
            # File removed on disk — prune any stale graph state we still hold for
            # it so deleted symbols stop surfacing in search/callers/references.
            if await self.store.get_file_info(path_str) is not None:
                await self.store.delete_file(path_str)
                logger.info("Pruned deleted file from graph: %s", path_str)
            return "ok"

        info = await self.store.get_file_info(path_str)

        if info is not None:
            if info["mtime"] == stat.st_mtime and info["size"] == stat.st_size:
                # Fast path: unchanged
                if semantic and info["status"] == "skeleton":
                    await self._in_flight.get_or_create(
                        path_str,
                        lambda: self._index_semantic(file_path, stat),
                    )
                    refreshed = await self.store.get_file_info(path_str)
                    return refreshed["status"] if refreshed else info["status"]
                return info["status"]

            # mtime/size differ — check content hash to avoid false positives
            content_hash = _file_hash(path_str)
            if content_hash == info["hash"]:
                await self.store.update_file_mtime(path_str, stat.st_mtime, stat.st_size)
                return info["status"]

        # Slow path: file changed or not yet indexed
        await self._in_flight.get_or_create(
            path_str,
            lambda: self._index_file(file_path, stat, semantic=semantic),
        )
        new_info = await self.store.get_file_info(path_str)
        return new_info["status"] if new_info else "skeleton"

    async def _dependency_stale(self, path_str: str) -> bool:
        """Return True if any file *path_str* imports has changed/disappeared on disk.

        Cheap on the common path: a stat per recorded dependency, hashing only when
        mtime/size already differ (so a mere ``touch`` does not trigger a false stale).
        """
        for imported in await self.store.get_imported_files(path_str):
            info = await self.store.get_file_info(imported)
            if info is None:
                continue  # never indexed as a tracked file; nothing to compare
            try:
                st = Path(imported).stat()
            except OSError:
                return True  # a dependency was deleted out from under us
            if info["mtime"] == st.st_mtime and info["size"] == st.st_size:
                continue
            if _file_hash(imported) != info["hash"]:
                return True
        return False

    # ------------------------------------------------------------------
    # Internal indexing
    # ------------------------------------------------------------------

    async def _index_file(self, file_path: Path, stat: os.stat_result, *, semantic: bool) -> None:
        """Phase 1 (skeleton) + optional Phase 2 (semantic)."""
        await self._index_skeleton(file_path, stat)
        if semantic:
            await self._index_semantic(file_path, stat)

    async def _index_skeleton(self, file_path: Path, stat: os.stat_result) -> None:
        """Phase 1: structure-only using NullResolver."""
        lang = _detect_language(file_path)
        if lang is None:
            return

        null_adapter = self._null_adapter(lang)
        if null_adapter is None:
            return

        path_str = str(file_path.resolve())
        file_hash = _file_hash(path_str)

        async with self._semaphore:
            graph = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: null_adapter.analyze(self.project_root, files=[file_path]),
            )
        graph = _normalize_graph_paths(graph, self.project_root)

        await self.store.apply_patch(
            graph,
            path_str,
            file_hash,
            stat.st_mtime,
            stat.st_size,
            "skeleton",
            lang,
        )

    async def _index_semantic(self, file_path: Path, stat: os.stat_result) -> None:
        """Phase 2: full semantic indexing."""
        lang = _detect_language(file_path)
        if lang is None:
            return

        adapter = self._adapter(lang)
        if adapter is None:
            return

        path_str = str(file_path.resolve())
        file_hash = _file_hash(path_str)

        # Guard against stale result: re-check hash before applying
        async with self._semaphore:
            graph = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: adapter.analyze(self.project_root, files=[file_path]),
            )
        graph = _normalize_graph_paths(graph, self.project_root)

        current_hash = _file_hash(path_str)
        if current_hash != file_hash:
            logger.debug("File %s changed during semantic index; discarding.", path_str)
            return

        resolver_status = ResolverStatus.from_value(graph.metadata.get(RESOLVER_STATUS_KEY, "ok"))
        file_status = _resolver_to_file_status(resolver_status)

        await self.store.apply_patch(
            graph,
            path_str,
            file_hash,
            stat.st_mtime,
            stat.st_size,
            file_status,
            lang,
        )


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _normalize_graph_paths(graph: GraphLens, project_root: Path) -> GraphLens:
    """Return a copy of *graph* with every ``node.file_path`` resolved to absolute.

    graphlens adapters emit mixed path forms — FILE/MODULE nodes carry paths
    relative to the project root while symbol nodes carry absolute paths.
    Persisting them as-is silently drops the relative ones (``os.stat`` fails when
    the process cwd is not the project root) and pollutes the ``files`` table with
    relative/absolute duplicates of the same file. Normalising up front keys
    everything on one absolute form, matching ``ensure_fresh`` which looks files up
    by ``Path.resolve()``. ``Node`` is frozen, so we rebuild via ``replace``.
    """
    normalized = GraphLens()
    for node in graph.nodes.values():
        norm_node = node
        if node.file_path:
            p = Path(node.file_path)
            if not p.is_absolute():
                p = project_root / p
            norm_node = dataclasses.replace(node, file_path=str(p.resolve()))
        normalized.add_node(norm_node)
    for rel in graph.relations:
        normalized.add_relation(rel)
    normalized.metadata.update(graph.metadata)
    return normalized


async def _persist_graph(
    store: SqliteStore,
    graph: GraphLens,
    lang: str,
    file_status: str,
) -> None:
    """Persist all nodes/edges from a full-index graph, grouped by file."""
    files_in_graph: dict[str, list] = {}
    for node in graph.nodes.values():
        if node.file_path:
            files_in_graph.setdefault(node.file_path, [])

    for file_path in files_in_graph:
        try:
            stat = Path(file_path).stat()
        except OSError:
            logger.warning("Skipping %s during persist: cannot stat (not on disk)", file_path)
            continue
        file_hash = _file_hash(file_path)
        sub = graph.subgraph_for_file(file_path)
        await store.apply_patch(
            sub,
            file_path,
            file_hash,
            stat.st_mtime,
            stat.st_size,
            file_status,
            lang,
        )


def _synthesize_cross_language_edges(graph: GraphLens) -> list[tuple[str, str, str]]:
    """Synthesize COMMUNICATES_WITH edges between nodes sharing BOUNDARY targets."""
    boundary_kind = "boundary"
    communicates = RelationKind.COMMUNICATES_WITH.value

    # boundary_id -> list of (node_id, role) where role is 'exposes' or 'consumes'
    boundary_ports: dict[str, list[tuple[str, str]]] = {}
    for rel in graph.relations:
        if rel.kind not in (RelationKind.EXPOSES, RelationKind.CONSUMES):
            continue
        target = graph.nodes.get(rel.target_id)
        if target is None or target.kind.value != boundary_kind:
            continue
        boundary_ports.setdefault(rel.target_id, []).append((rel.source_id, rel.kind.value))

    edges: set[tuple[str, str, str]] = set()
    for boundary_id, ports in boundary_ports.items():
        exposers = {p[0] for p in ports if p[1] == "exposes"}
        consumers = {p[0] for p in ports if p[1] == "consumes"}
        fanout = len(exposers) * len(consumers)
        if fanout > _MAX_BOUNDARY_FANOUT:
            # A hub boundary (e.g. one queue topic with hundreds of consumers) would
            # otherwise materialize a quadratic edge blow-up. Skip the synthesized
            # pairwise edges; the boundary-based query still resolves connections.
            logger.warning(
                "Skipping COMMUNICATES_WITH synthesis for boundary %s: fan-out %d exceeds %d",
                boundary_id,
                fanout,
                _MAX_BOUNDARY_FANOUT,
            )
            continue
        for src in exposers:
            for dst in consumers:
                if src != dst:
                    edges.add((src, dst, communicates))
                    edges.add((dst, src, communicates))

    return sorted(edges)


def _detect_language(file_path: Path) -> str | None:
    return _get_ext_map().get(file_path.suffix)


def _file_hash(path: str) -> str:
    h = hashlib.sha256()
    try:
        with Path(path).open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except OSError:
        logger.debug("Could not hash %s (missing/unreadable)", path)
    return h.hexdigest()


def _resolver_to_file_status(status: ResolverStatus) -> str:
    if status == ResolverStatus.OK:
        return "ok"
    if status == ResolverStatus.DEGRADED:
        return "degraded"
    return "skeleton"
