"""
Semantic retrieval and clustering over the code graph.

This is the optional ``[semantic]`` layer that lets an agent search the
codebase *by meaning* (not just by symbol name or text) and navigate its
semantic neighborhoods — the capabilities that otherwise push agents back
to ``grep``. It is built on two pieces, both pulled in only by the
``semantic`` extra so a base install stays dependency-light:

* **semble** — a static-embedding + BM25 hybrid retriever. We wrap its
  index so a semantic hit (a file + line range) can be bridged back to the
  graph's *node ids* (see :meth:`SqliteStore.nodes_overlapping`), letting a
  "found by meaning" result pivot straight into ``get_callers`` /
  ``get_callees``.
* **model2vec + scikit-learn** — we embed each symbol node and cluster the
  vectors (HDBSCAN) into labeled semantic zones ("auth", "serialization",
  …), something semble itself does not provide.

Every heavy import is lazy and guarded. Importing this module never fails
on a base install; instead :func:`semantic_availability` reports *why* the
layer is off (extra not installed) and a build/query reports at runtime if
the embedding model cannot be fetched (offline, blocked egress, no token),
so the core graph server keeps working regardless.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

logger = logging.getLogger(__name__)

# The code-specialized static embedding model semble uses; reused here for
# node clustering so search and clusters share one vector space (and one
# downloaded/cached model).
MODEL_ID = "minishlab/potion-code-16M"

# Clustering knobs. HDBSCAN leaves sparse nodes unclustered (label -1); only
# dense semantic zones become clusters, which is what we want for a navigation
# map rather than forcing every symbol into a bucket.
_MIN_CLUSTER_SIZE = 3
_MAX_LABEL_TERMS = 4
# Above this node count, embedding+clustering the whole graph is skipped unless
# explicitly forced — keeps a one-shot full index from stalling on a huge repo.
_MAX_CLUSTER_NODES = 50_000

# Metadata keys graphlens adapters attach to a definition (mirror tools.py).
_SIGNATURE_KEYS = ("signature", "sig")
_DOCSTRING_KEYS = ("docstring", "doc", "documentation")

# Generic identifier tokens that make for useless cluster labels.
_LABEL_STOPWORDS = frozenset(
    {
        "get",
        "set",
        "is",
        "to",
        "from",
        "the",
        "self",
        "cls",
        "init",
        "new",
        "value",
        "data",
        "obj",
        "object",
        "fn",
        "func",
        "method",
        "class",
        "test",
        "tests",
        "impl",
        "base",
        "main",
        "run",
        "handle",
        "handler",
        "do",
        "make",
    }
)


@dataclass(frozen=True)
class Availability:
    """Whether the semantic layer can run, with a reason when it cannot."""

    ok: bool
    reason: str | None = None


@dataclass(frozen=True)
class SemanticHit:
    """One semantic search hit: a code chunk plus its relevance score."""

    file_path: str
    start_line: int
    end_line: int
    content: str
    score: float
    language: str | None = None


@dataclass(frozen=True)
class SemanticResponse:
    """Result of a semantic query: hits when available, else a reason."""

    available: bool
    hits: list[SemanticHit] = field(default_factory=list)
    reason: str | None = None


@dataclass(frozen=True)
class ClusterComputation:
    """Computed clusters and node→cluster assignments, ready to persist."""

    clusters: list[dict[str, Any]]
    assignments: list[dict[str, Any]]


_INSTALL_HINT = (
    "Semantic search is not available: install the optional extra with "
    "`uv sync --extra semantic` (or `pip install 'graphlens-mcp[semantic]'`)."
)


def semantic_availability() -> Availability:
    """
    Report whether the ``[semantic]`` extra is importable.

    Checks only that the heavy packages import — not that the embedding
    model can be fetched, which is discovered (and reported) at build/query
    time, since it depends on runtime network/egress.
    """
    try:
        import semble  # noqa: F401, PLC0415
        import sklearn  # noqa: F401, PLC0415
    except ImportError:
        return Availability(ok=False, reason=_INSTALL_HINT)
    return Availability(ok=True)


def _is_network_error(exc: BaseException) -> bool:
    """Tell whether a model fetch failed for a network/egress reason."""
    text = f"{type(exc).__name__}: {exc}".lower()
    needles = (
        "proxy",
        "connection",
        "timed out",
        "timeout",
        "403",
        "407",
        "ssl",
        "certificate",
        "network",
        "resolve",
        "offline",
        "could not download",
        "failed to fetch",
        "huggingface",
    )
    return any(n in text for n in needles)


def _model_error_reason(exc: BaseException) -> str:
    if _is_network_error(exc):
        return (
            "Semantic search is installed but the embedding model "
            f"({MODEL_ID}) could not be fetched: {type(exc).__name__}. "
            "Check network/egress access to the model host, or set HF_TOKEN."
        )
    return (
        f"Semantic search failed to load the embedding model: "
        f"{type(exc).__name__}: {exc}"
    )


class SemanticIndex:
    """
    Lazy owner of the semble retrieval index for one project root.

    The index is built on first use and persisted to a sidecar under
    ``.graphlens`` so a restart can reload it without re-chunking the whole
    corpus. Incremental edits flip a dirty flag (the watcher cannot patch
    semble's index in place), so the next query rebuilds from the current
    tree. All blocking work (build, embed, cluster) is run off the event
    loop. When the extra is missing or the model is unreachable the index
    stays unavailable and every query returns a structured reason rather
    than raising.
    """

    def __init__(self, project_root: Path, sidecar_path: Path) -> None:
        """Bind to *project_root*; persist the index at *sidecar_path*."""
        self._root = project_root
        self._sidecar = sidecar_path
        self._index: Any = None
        self._dirty = True
        self._lock = asyncio.Lock()
        # Sticky reason once the model proves unreachable, so we don't retry
        # a slow/blocked fetch on every single query within a session.
        self._runtime_reason: str | None = None

    def mark_dirty(self) -> None:
        """Flag the index stale so the next query rebuilds it."""
        self._dirty = True

    @property
    def availability(self) -> Availability:
        """Import-level availability (the extra installed?)."""
        base = semantic_availability()
        if not base.ok:
            return base
        if self._runtime_reason is not None:
            return Availability(ok=False, reason=self._runtime_reason)
        return Availability(ok=True)

    # ------------------------------------------------------------------
    # Build / load
    # ------------------------------------------------------------------

    async def _ensure_index(self) -> Availability:
        """Build or reload the semble index if needed; return availability."""
        base = semantic_availability()
        if not base.ok:
            return base
        if self._runtime_reason is not None:
            return Availability(ok=False, reason=self._runtime_reason)
        if self._index is not None and not self._dirty:
            return Availability(ok=True)

        async with self._lock:
            # Re-check under the lock: a concurrent caller may have built it.
            if self._index is not None and not self._dirty:
                return Availability(ok=True)
            try:
                index = await asyncio.to_thread(self._build_or_load_blocking)
            except Exception as exc:
                reason = _model_error_reason(exc)
                logger.warning("Semantic index build failed: %s", reason)
                self._runtime_reason = reason
                return Availability(ok=False, reason=reason)
            self._index = index
            self._dirty = False
            return Availability(ok=True)

    def _build_or_load_blocking(self) -> Any:
        """Build the index from the tree (blocking); persist best-effort."""
        from semble import ContentType, SembleIndex  # noqa: PLC0415

        # A dirty rebuild must reflect the current tree, so build fresh rather
        # than trusting a stale sidecar. A clean cold start may reload it.
        if not self._dirty and self._sidecar.exists():
            try:
                return SembleIndex.load_from_disk(self._sidecar)
            except Exception as exc:
                logger.debug("Sidecar reload failed, rebuilding: %s", exc)

        index = SembleIndex.from_path(self._root, content=(ContentType.CODE,))
        # Persistence is an optimization, never load-bearing.
        try:
            self._sidecar.parent.mkdir(parents=True, exist_ok=True)
            index.save(self._sidecar)
        except Exception as exc:
            logger.debug("Sidecar save failed (non-fatal): %s", exc)
        return index

    async def build(self) -> Availability:
        """
        Eagerly (re)build the index — called by the full-index pipeline.

        Returns availability so the pipeline can record whether the
        semantic phase actually completed (vs. degraded offline).
        """
        self._dirty = True
        self._runtime_reason = None
        return await self._ensure_index()

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        top_k: int,
        *,
        filter_paths: list[str] | None = None,
        max_snippet_lines: int | None = None,
    ) -> SemanticResponse:
        """Run a semantic+lexical search; return hits or a reason."""
        avail = await self._ensure_index()
        if not avail.ok:
            return SemanticResponse(available=False, reason=avail.reason)
        try:
            results = await asyncio.to_thread(
                lambda: self._index.search(
                    query,
                    top_k=top_k,
                    filter_paths=filter_paths,
                    max_snippet_lines=max_snippet_lines,
                )
            )
        except Exception as exc:
            reason = _model_error_reason(exc)
            logger.warning("Semantic search failed: %s", reason)
            return SemanticResponse(available=False, reason=reason)
        return SemanticResponse(
            available=True, hits=[_to_hit(r) for r in results]
        )

    async def find_related(
        self,
        *,
        file_path: str,
        start_line: int,
        end_line: int,
        content: str,
        language: str | None,
        top_k: int,
        max_snippet_lines: int | None = None,
    ) -> SemanticResponse:
        """Find chunks semantically similar to a given code span."""
        avail = await self._ensure_index()
        if not avail.ok:
            return SemanticResponse(available=False, reason=avail.reason)
        try:
            from semble import Chunk  # noqa: PLC0415

            source = Chunk(
                content=content,
                file_path=file_path,
                start_line=start_line,
                end_line=end_line,
                language=language,
            )
            results = await asyncio.to_thread(
                lambda: self._index.find_related(
                    source,
                    top_k=top_k,
                    max_snippet_lines=max_snippet_lines,
                )
            )
        except Exception as exc:
            reason = _model_error_reason(exc)
            logger.warning("find_related failed: %s", reason)
            return SemanticResponse(available=False, reason=reason)
        return SemanticResponse(
            available=True, hits=[_to_hit(r) for r in results]
        )

    # ------------------------------------------------------------------
    # Clustering
    # ------------------------------------------------------------------

    async def compute_clusters(
        self, nodes: list[dict[str, Any]]
    ) -> ClusterComputation | None:
        """
        Embed *nodes* and cluster them into labeled semantic zones.

        Returns None when the layer is unavailable (extra missing or model
        unreachable) or there is nothing to cluster, so the caller can skip
        the cluster phase without treating it as a hard failure. Runs off
        the event loop; the heavy numeric work is delegated to
        :func:`_cluster_blocking`.
        """
        if not semantic_availability().ok:
            return None
        usable = [n for n in nodes if n.get("id")]
        if len(usable) < _MIN_CLUSTER_SIZE:
            return None
        if len(usable) > _MAX_CLUSTER_NODES:
            logger.warning(
                "Skipping clustering: %d nodes exceeds cap %d",
                len(usable),
                _MAX_CLUSTER_NODES,
            )
            return None
        try:
            return await asyncio.to_thread(self._cluster_blocking, usable)
        except Exception as exc:
            reason = _model_error_reason(exc)
            logger.warning("Clustering failed (non-fatal): %s", reason)
            self._runtime_reason = self._runtime_reason or reason
            return None

    def _cluster_blocking(
        self, nodes: list[dict[str, Any]]
    ) -> ClusterComputation:
        """Embed + HDBSCAN-cluster *nodes* (blocking, CPU-bound)."""
        import numpy as np  # noqa: PLC0415
        from model2vec import StaticModel  # noqa: PLC0415
        from sklearn.cluster import HDBSCAN  # noqa: PLC0415

        model = StaticModel.from_pretrained(MODEL_ID)
        texts = [_embedding_text(n) for n in nodes]
        vectors = np.asarray(model.encode(texts), dtype=np.float32)
        # L2-normalize so Euclidean distance on the unit sphere tracks cosine.
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        unit = vectors / norms

        labels = HDBSCAN(
            min_cluster_size=_MIN_CLUSTER_SIZE,
            metric="euclidean",
            copy=True,
        ).fit_predict(unit)

        return _assemble_clusters(nodes, unit, labels)


# ------------------------------------------------------------------
# Module-level helpers (pure; unit-testable without the model)
# ------------------------------------------------------------------


def _to_hit(result: Any) -> SemanticHit:
    """Convert a semble ``SearchResult`` into our transport-stable hit."""
    chunk = result.chunk
    return SemanticHit(
        file_path=chunk.file_path,
        start_line=chunk.start_line,
        end_line=chunk.end_line,
        content=chunk.content,
        score=float(result.score),
        language=chunk.language,
    )


def _first_meta(
    metadata_json: str | None, keys: tuple[str, ...]
) -> str | None:
    """Return the first present string value among *keys* in metadata JSON."""
    if not metadata_json:
        return None
    try:
        meta = json.loads(metadata_json)
    except (ValueError, TypeError):
        return None
    if not isinstance(meta, dict):
        return None
    for key in keys:
        value = meta.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _embedding_text(node: dict[str, Any]) -> str:
    """Build the text embedded for a node: name + signature + docstring."""
    parts: list[str] = [
        str(node.get("qualified_name") or node.get("name") or "")
    ]
    sig = _first_meta(node.get("metadata_json"), _SIGNATURE_KEYS)
    if sig:
        parts.append(sig)
    doc = _first_meta(node.get("metadata_json"), _DOCSTRING_KEYS)
    if doc:
        # Keep the embedding focused on the summary line of the docstring.
        parts.append(doc.strip().splitlines()[0][:200])
    return "\n".join(p for p in parts if p)


def _split_identifier(name: str) -> list[str]:
    """Split a dotted/camelCase/snake_case identifier into lowercase tokens."""
    tokens: list[str] = []
    for part in re.split(r"[^0-9A-Za-z]+", name):
        if not part:
            continue
        for tok in re.findall(
            r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+", part
        ):
            tokens.append(tok.lower())
    return tokens


def _label_for(names: Iterable[str]) -> tuple[str, list[str]]:
    """
    Derive a short cluster label and its top terms from member names.

    Tokenizes each member's name into identifier sub-tokens, drops generic
    stopwords, and ranks by frequency. Returns ``(label, terms)`` where the
    label joins the top terms (falling back to a generic label if nothing
    distinctive survives).
    """
    counter: Counter[str] = Counter()
    for name in names:
        for tok in _split_identifier(name):
            if len(tok) > 1 and tok not in _LABEL_STOPWORDS:
                counter[tok] += 1
    terms = [t for t, _ in counter.most_common(_MAX_LABEL_TERMS)]
    label = ", ".join(terms) if terms else "misc"
    return label, terms


def _assemble_clusters(
    nodes: list[dict[str, Any]],
    unit_vectors: Any,
    labels: Any,
) -> ClusterComputation:
    """
    Build cluster rows + assignments from HDBSCAN labels and unit vectors.

    Noise points (label ``-1``) are left unclustered. Each member's score is
    the cosine similarity to its cluster centroid (vectors are unit-norm, so
    that is just the dot product), giving a tightness signal that orders
    members and lets callers gauge how representative a member is.
    """
    import numpy as np  # noqa: PLC0415

    by_label: dict[int, list[int]] = {}
    for idx, raw in enumerate(labels):
        label = int(raw)
        if label < 0:
            continue
        by_label.setdefault(label, []).append(idx)

    clusters: list[dict[str, Any]] = []
    assignments: list[dict[str, Any]] = []
    # Renumber to dense, size-sorted ids (largest cluster = 1) for stable,
    # friendly references regardless of HDBSCAN's internal label numbers.
    ordered: list[list[int]] = list(by_label.values())
    ordered.sort(key=len, reverse=True)
    for new_id, members in enumerate(ordered, start=1):
        names = [
            nodes[i].get("qualified_name") or nodes[i].get("name") or ""
            for i in members
        ]
        label, terms = _label_for(names)
        centroid = unit_vectors[members].mean(axis=0)
        cnorm = float(np.linalg.norm(centroid)) or 1.0
        centroid = centroid / cnorm
        clusters.append(
            {
                "id": new_id,
                "label": label,
                "size": len(members),
                "terms": terms,
            }
        )
        for i in members:
            score = float(np.dot(unit_vectors[i], centroid))
            assignments.append(
                {
                    "node_id": nodes[i]["id"],
                    "cluster_id": new_id,
                    "score": score,
                }
            )
    return ClusterComputation(clusters=clusters, assignments=assignments)
