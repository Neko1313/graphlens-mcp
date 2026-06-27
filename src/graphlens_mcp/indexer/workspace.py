"""
Workspace orchestration.

Indexing, watcher-driven freshness, and cross-language linking. The graph is
kept current by a single mechanism — a filesystem **watcher** (watchfiles).
When a file changes the watcher re-indexes the *connected set* (the changed
file plus the files that import it and the files it imports) with a full
analyze, so cross-file edges are rebuilt correctly rather than left partial.
There is no polling and no structure-only "skeleton" phase: every (re)index
produces the full graph the resolver can give.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from graphlens import (
    RESOLVER_STATUS_KEY,
    GraphLens,
    LanguageAdapter,
    RelationKind,
    ResolverStatus,
    adapter_registry,
)
from watchfiles import awatch

from graphlens_mcp.indexer.concurrency import (
    MAX_CONCURRENT_RESOLVERS,
    InFlightRegistry,
)
from graphlens_mcp.indexer.resolvers import doctor, get_adapter
from graphlens_mcp.store.sqlite_store import SqliteStore

if TYPE_CHECKING:
    import os
    from collections.abc import Iterable

# Built lazily on first use from adapter.file_extensions()
_EXT_TO_LANG: dict[str, str] | None = None


def _get_ext_map() -> dict[str, str]:
    global _EXT_TO_LANG  # noqa: PLW0603 — process-wide cache of adapter exts
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

# Directories skipped when discovering source files on disk.
_EXCLUDED_DIRS = frozenset(
    {
        _GRAPHLENS_DIR,
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        ".venv",
        "venv",
        "node_modules",
        "dist",
        "build",
        ".eggs",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
    }
)

# Above this exposer-by-consumer product a single boundary stops
# synthesizing pairwise COMMUNICATES_WITH edges (a hub topic would
# otherwise blow up quadratically).
_MAX_BOUNDARY_FANOUT = 2000


def default_db_path(project_root: Path) -> Path:
    """Return the default graph database location for *project_root*."""
    return project_root / _GRAPHLENS_DIR / _DB_NAME


class Workspace:
    """Manages the indexing lifecycle for a project root."""

    def __init__(self, store: SqliteStore, project_root: Path) -> None:
        """Bind the workspace to *store* and *project_root*."""
        self.store = store
        self.project_root = project_root
        self._in_flight = InFlightRegistry()
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_RESOLVERS)
        # One long-lived adapter per language for the Workspace lifetime,
        # so the expensive adapter/resolver init (TS install cache,
        # resolver objects) is reused across the full index and every
        # re-index. (Invariant: resolver off the request hot path.)
        self._adapters: dict[str, LanguageAdapter | None] = {}
        self._watch_task: asyncio.Task[None] | None = None
        # Serializes reindex_connected so a watcher re-index and an on-access
        # re-index of overlapping files cannot interleave read/prune/write.
        self._reindex_lock = asyncio.Lock()

    @classmethod
    async def create(cls, project_root: Path, db_path: Path) -> Workspace:
        """Open store at *db_path*; return a Workspace for *project_root*."""
        store = await SqliteStore.create(db_path)
        return cls(store, project_root)

    def _adapter(self, lang: str) -> LanguageAdapter | None:
        """Return the pooled real adapter for *lang*, creating it once."""
        if lang not in self._adapters:
            self._adapters[lang] = get_adapter(lang)
        return self._adapters[lang]

    # ------------------------------------------------------------------
    # Filesystem watcher (the single freshness mechanism)
    # ------------------------------------------------------------------

    def start_watching(self) -> None:
        """
        Start the filesystem watcher that keeps the graph fresh.

        Idempotent — a second call is a no-op. The watcher re-indexes the
        connected set of every changed file, so the graph self-heals even
        when the agent never queries the edited file.
        """
        if self._watch_task is not None:
            return
        self._watch_task = asyncio.create_task(self._watch_loop())

    async def stop_watching(self) -> None:
        """Cancel the watcher task if it is running."""
        task = self._watch_task
        self._watch_task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _watch_loop(self) -> None:
        """Re-index the connected set of every changed source file."""
        exts = set(_get_ext_map())
        while True:
            try:
                async for changes in awatch(self.project_root):
                    paths: set[str] = set()
                    for _change, raw in changes:
                        p = Path(raw)
                        if _GRAPHLENS_DIR in p.parts or p.suffix not in exts:
                            continue
                        paths.add(await _aresolve(p))
                    if paths:
                        await self.reindex_connected(paths)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A re-index error or a transient watcher error must not kill
                # the loop for good; log and re-establish the watch shortly.
                logger.warning("Watcher error; restarting: %s", exc)
                await asyncio.sleep(1.0)

    async def close(self) -> None:
        """
        Stop the watcher, shut down pooled resolvers, and close the store.

        Best-effort resolver shutdown: prefer a public
        ``close``/``shutdown``/``stop`` on the adapter, falling back to its
        private ``_resolver``. Idempotent — safe to call more than once.
        """
        await self.stop_watching()
        for adapter in self._adapters.values():
            if adapter is None:
                continue
            self._shutdown_one(adapter)
        self._adapters.clear()
        await self.store.close()

    @staticmethod
    def _shutdown_one(adapter: LanguageAdapter) -> None:
        # Try the adapter's own lifecycle hook first (public API), then
        # the resolver's.
        for target in (adapter, getattr(adapter, "_resolver", None)):
            if target is None:
                continue
            for method in ("shutdown", "close", "stop"):
                fn = getattr(target, method, None)
                if callable(fn):
                    try:
                        fn()
                    except (
                        Exception
                    ) as exc:  # pragma: no cover - best-effort cleanup
                        logger.debug(
                            "Shutdown via %s.%s failed: %s",
                            target,
                            method,
                            exc,
                        )
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

        # Analyze every applicable language concurrently — each analyze is a
        # blocking call run in the executor and gated by the resolver
        # semaphore. Persisting and merging stay sequential afterwards
        # (writes serialize on the store's write lock and merge() mutates
        # shared in-memory state), so only the slow parse/resolve phase is
        # parallelized.
        targets = [
            lang
            for lang in languages
            if report.get(lang) is not None
            and (a := self._adapter(lang)) is not None
            and a.can_handle(self.project_root)
        ]
        graphs = await asyncio.gather(
            *(self._analyze_language(lang) for lang in targets)
        )

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
        # boundary nodes) that the per-file ownership filter would otherwise
        # drop.
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
        """Run a full analyze for *lang* in the executor, semaphore-gated."""
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
    # Startup reconcile (discover files changed while the server was down)
    # ------------------------------------------------------------------

    async def reconcile(self) -> int:
        """
        Reconcile the graph with the source files currently on disk.

        The watcher only sees events while it runs, so files created, deleted
        or edited *while the server was down* are invisible to it. This
        one-shot scan (run at ``serve`` start, not on a timer) indexes new
        files, prunes vanished ones, and refreshes any tracked file whose
        bytes changed, then re-links each via :meth:`reindex_connected`.
        Returns the number of files that needed work.
        """
        disk = await self._discover_source_files()
        db_paths = {row["path"] for row in await self.store.list_files()}

        # Empty store (e.g. a fresh DB): the parallel full index is faster and
        # simpler than feeding the whole tree through the connected-set path.
        if not db_paths:
            if disk:
                await self.full_index()
            return len(disk)

        to_refresh: set[str] = db_paths - disk  # deleted on disk
        for path_str in disk:
            if path_str not in db_paths:
                to_refresh.add(path_str)  # new file
                continue
            info = await self.store.get_file_info(path_str)
            try:
                stat = await _astat(path_str)
            except OSError:
                continue
            if info is None:
                to_refresh.add(path_str)
                continue
            changed = (
                info["mtime"] != stat.st_mtime or info["size"] != stat.st_size
            )
            if changed and await _ahash(path_str) != info["hash"]:
                to_refresh.add(path_str)  # edited while the server was down

        if to_refresh:
            await self.reindex_connected(to_refresh)
        return len(to_refresh)

    async def _discover_source_files(self) -> set[str]:
        """Return absolute paths of all known-language files under the root."""
        exts = set(_get_ext_map())

        def _walk() -> set[str]:
            found: set[str] = set()
            for path in self.project_root.rglob("*"):
                if path.suffix not in exts or not path.is_file():
                    continue
                rel_parts = path.relative_to(self.project_root).parts
                if _EXCLUDED_DIRS & set(rel_parts):
                    continue
                found.add(str(path.resolve()))
            return found

        return await asyncio.to_thread(_walk)

    # ------------------------------------------------------------------
    # Connected-set re-index (watcher + on-access)
    # ------------------------------------------------------------------

    async def reindex_connected(self, changed: Iterable[str]) -> None:
        """
        Re-index every changed file together with its connected files.

        The connected set is the changed file plus the files that import it
        (so their cross-file edges into the new definition are rebuilt) and
        the files it imports. Analyzing the set together lets the resolver
        re-link calls across those files, so the result is a full graph for
        the affected region — not a single-file approximation. Deleted files
        are pruned and their importers refreshed. Serialized by
        ``_reindex_lock`` so a watcher re-index and an on-access re-index of
        overlapping files cannot interleave their read/prune/write phases.
        """
        async with self._reindex_lock:
            affected: set[str] = set()
            deleted: set[str] = set()
            for raw in changed:
                path_str = await _aresolve(Path(raw))
                # Connections come from the *current* graph, before pruning.
                connected = set(await self.store.get_importer_files(path_str))
                connected |= set(await self.store.get_imported_files(path_str))
                affected |= connected
                if await _exists(path_str):
                    affected.add(path_str)
                else:
                    deleted.add(path_str)

            for path_str in deleted:
                await self.store.delete_file(path_str)
                logger.info("Pruned deleted file from graph: %s", path_str)

            by_lang: dict[str, list[Path]] = {}
            for path_str in affected - deleted:
                if not await _exists(path_str):
                    continue
                lang = _detect_language(Path(path_str))
                if lang is not None:
                    by_lang.setdefault(lang, []).append(Path(path_str))

            for lang, paths in by_lang.items():
                await self._reindex_lang(lang, paths)

    async def _reindex_lang(self, lang: str, paths: list[Path]) -> None:
        """Full-analyze *paths* of one language and persist each file."""
        adapter = self._adapter(lang)
        if adapter is None:
            return
        async with self._semaphore:
            graph = await asyncio.get_running_loop().run_in_executor(
                None, lambda: adapter.analyze(self.project_root, files=paths)
            )
        graph = _normalize_graph_paths(graph, self.project_root)
        file_status = _resolver_to_file_status(
            ResolverStatus.from_value(
                graph.metadata.get(RESOLVER_STATUS_KEY, "ok")
            )
        )
        for path in paths:
            path_str = await _aresolve(path)
            try:
                stat = await _astat(path_str)
            except OSError:
                continue
            sub = graph.subgraph_for_file(path_str)
            await self.store.apply_patch(
                sub,
                path_str,
                await _ahash(path_str),
                stat.st_mtime,
                stat.st_size,
                file_status,
                lang,
            )

    # ------------------------------------------------------------------
    # On-access freshness (correctness backstop; watcher is proactive)
    # ------------------------------------------------------------------

    async def ensure_fresh(self, file_path: Path) -> str:
        """
        Ensure *file_path* is current and return its graph status.

        The watcher keeps the graph fresh proactively; this is the on-access
        guarantee for a tool that touches a file before the watcher has
        processed it (or when no watcher runs). A changed or new file
        triggers a connected-set re-index. Returns 'ok' or 'degraded'.
        """
        path_str = await _aresolve(file_path)
        try:
            stat = await _astat(path_str)
        except OSError:
            # Removed on disk — prune (and refresh its importers).
            if await self.store.get_file_info(path_str) is not None:
                await self.reindex_connected({path_str})
            return "ok"

        info = await self.store.get_file_info(path_str)
        if info is not None:
            if info["mtime"] == stat.st_mtime and info["size"] == stat.st_size:
                return info["status"]
            if await _ahash(path_str) == info["hash"]:
                await self.store.update_file_mtime(
                    path_str, stat.st_mtime, stat.st_size
                )
                return info["status"]

        await self._in_flight.get_or_create(
            path_str, lambda: self.reindex_connected({path_str})
        )
        new_info = await self.store.get_file_info(path_str)
        return new_info["status"] if new_info else "degraded"


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


async def _astat(path: str | Path) -> os.stat_result:
    """Stat *path* off the event loop (keeps blocking FS out of async)."""
    return await asyncio.to_thread(Path(path).stat)


async def _ahash(path: str) -> str:
    """Content-hash *path* off the event loop."""
    return await asyncio.to_thread(_file_hash, path)


async def _aresolve(path: Path) -> str:
    """Resolve *path* to an absolute string off the event loop."""
    return str(await asyncio.to_thread(path.resolve))


async def _exists(path: str) -> bool:
    """Return True if *path* exists on disk (checked off the event loop)."""
    return await asyncio.to_thread(Path(path).exists)


def _normalize_graph_paths(graph: GraphLens, project_root: Path) -> GraphLens:
    """
    Return a copy of *graph* with each ``node.file_path`` made absolute.

    graphlens adapters emit mixed path forms — FILE/MODULE nodes carry paths
    relative to the project root while symbol nodes carry absolute paths.
    Persisting them as-is silently drops the relative ones (``os.stat`` fails
    when the process cwd is not the project root) and pollutes the ``files``
    table with relative/absolute duplicates of the same file. Normalising up
    front keys everything on one absolute form, matching ``ensure_fresh``
    which looks files up by ``Path.resolve()``. ``Node`` is frozen, so we
    rebuild via ``replace``.
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
            stat = await _astat(file_path)
        except OSError:
            logger.warning(
                "Skipping %s during persist: cannot stat (not on disk)",
                file_path,
            )
            continue
        file_hash = await _ahash(file_path)
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


def _synthesize_cross_language_edges(
    graph: GraphLens,
) -> list[tuple[str, str, str]]:
    """Synthesize COMMUNICATES_WITH between nodes sharing BOUNDARY targets."""
    boundary_kind = "boundary"
    communicates = RelationKind.COMMUNICATES_WITH.value

    # boundary_id -> list of (node_id, role) where role is 'exposes' or
    # 'consumes'
    boundary_ports: dict[str, list[tuple[str, str]]] = {}
    for rel in graph.relations:
        if rel.kind not in (RelationKind.EXPOSES, RelationKind.CONSUMES):
            continue
        target = graph.nodes.get(rel.target_id)
        if target is None or target.kind.value != boundary_kind:
            continue
        boundary_ports.setdefault(rel.target_id, []).append(
            (rel.source_id, rel.kind.value)
        )

    edges: set[tuple[str, str, str]] = set()
    for boundary_id, ports in boundary_ports.items():
        exposers = {p[0] for p in ports if p[1] == "exposes"}
        consumers = {p[0] for p in ports if p[1] == "consumes"}
        fanout = len(exposers) * len(consumers)
        if fanout > _MAX_BOUNDARY_FANOUT:
            # A hub boundary (e.g. one queue topic with hundreds of
            # consumers) would otherwise materialize a quadratic edge
            # blow-up. Skip the synthesized pairwise edges; the
            # boundary-based query still resolves connections.
            logger.warning(
                "Skipping COMMUNICATES_WITH synthesis for boundary %s: "
                "fan-out %d exceeds %d",
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
    # No more "skeleton": every index is a full analyze, so a non-ok resolver
    # (e.g. a missing language toolchain) is reported honestly as 'degraded'.
    return "ok" if status == ResolverStatus.OK else "degraded"
