"""
Semantic retrieval and clustering over the code graph.

Embeds graph *nodes* (function/method/class) directly with the model2vec
static model and stores the float32 vectors in SQLite.  Semantic search and
find_related do in-process cosine-similarity over a cached vector matrix —
no file chunking, no external retrieval service, no chunk→node bridge.
Each search hit is already a graph node, so the result pivots straight into
get_callers/get_callees without extra indirection.

model2vec, numpy, and scikit-learn are required dependencies.  The only
graceful-degradation path that remains is a model-download failure (network
outage, HF egress blocked), which is stored as a sticky reason and reported
via ``available=False`` so the caller can surface it without crashing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import model2vec
import numpy as np
from sklearn.cluster import HDBSCAN

if TYPE_CHECKING:
    from collections.abc import Iterable

    from graphlens_mcp.store.sqlite_store import SqliteStore

logger = logging.getLogger(__name__)

MODEL_ID = "minishlab/potion-code-16M"

_MIN_CLUSTER_SIZE = 3
_MAX_LABEL_TERMS = 4
# Above this node count, clustering is skipped — keeps a one-shot full index
# from stalling on a huge repo.
_MAX_CLUSTER_NODES = 50_000

_SIGNATURE_KEYS = ("signature", "sig")
_DOCSTRING_KEYS = ("docstring", "doc", "documentation")

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
    """One search hit: a graph node with a similarity score."""

    node_id: str
    kind: str
    name: str
    qualified_name: str
    file_path: str | None
    score: float


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


def _is_network_error(exc: BaseException) -> bool:
    """Tell whether a model fetch failed for a network/egress reason."""
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(
        n in text
        for n in (
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
    )


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
    Owns the node embedding cache for one project.

    :meth:`build` embeds all graph nodes with model2vec and persists the
    float32 vectors to SQLite (via :meth:`SqliteStore.store_embeddings`).
    Search and find_related do in-process cosine-similarity over a cached
    vector matrix loaded from the store on first use and reloaded after
    incremental edits via :meth:`mark_dirty`.

    All blocking work (model load, encode, cluster) runs off the event loop
    in a thread pool.  When the model is unreachable every method returns a
    structured reason rather than raising.
    """

    def __init__(self) -> None:  # noqa: D107
        self._dirty = True
        self._lock = asyncio.Lock()
        # Sticky reason once the model proves unreachable — avoids retrying a
        # slow/blocked fetch on every query within a session.
        self._runtime_reason: str | None = None
        # Cached model object — loaded once in build() and reused by search()
        # so we don't pay a model-deserialize cost on every query.
        self._model: Any = None
        # In-memory vector cache (rebuilt lazily from the store).
        self._vectors: Any = None  # np.ndarray (N, D), unit-normalised
        self._node_ids: list[str] = []
        self._node_meta: list[dict[str, Any]] = []

    def mark_dirty(self) -> None:
        """Invalidate the cache so the next query reloads from the store."""
        self._dirty = True

    @property
    def availability(self) -> Availability:
        """Runtime availability — ok unless the model failed to load."""
        if self._runtime_reason is not None:
            return Availability(ok=False, reason=self._runtime_reason)
        return Availability(ok=True)

    # ------------------------------------------------------------------
    # Build — embed all nodes, persist to SQLite
    # ------------------------------------------------------------------

    async def build(self, store: SqliteStore) -> Availability:
        """
        Embed all indexable nodes and write float32 vectors to the store.

        Called by the full-index pipeline.  On success reloads the in-memory
        cache so the next search is immediate.  Returns availability so the
        pipeline can checkpoint whether the semantic phase completed.
        """
        self._runtime_reason = None
        try:
            nodes = await store.get_nodes_for_clustering()
            if not nodes:
                self._dirty = False
                return Availability(ok=True)
            model, embedding_rows = await asyncio.to_thread(
                _embed_blocking, nodes
            )
            self._model = model
            await store.store_embeddings(embedding_rows)
            # Reload in-memory cache immediately so the next search is fast.
            rows = await store.get_embedding_rows()
            if rows:
                await asyncio.to_thread(self._load_blocking, rows)
            self._dirty = False
            return Availability(ok=True)
        except Exception as exc:
            reason = _model_error_reason(exc)
            logger.warning("Semantic build failed: %s", reason)
            self._runtime_reason = reason
            return Availability(ok=False, reason=reason)

    # ------------------------------------------------------------------
    # Vector cache management
    # ------------------------------------------------------------------

    async def _ensure_vectors(self, store: SqliteStore) -> Availability:
        """Load (or reload) the in-memory vector cache from the store."""
        if self._runtime_reason is not None:
            return Availability(ok=False, reason=self._runtime_reason)
        if not self._dirty and self._vectors is not None:
            return Availability(ok=True)

        async with self._lock:
            # Re-check under the lock: a concurrent caller may have loaded it.
            if not self._dirty and self._vectors is not None:
                return Availability(ok=True)
            try:
                rows = await store.get_embedding_rows()
                await asyncio.to_thread(self._load_blocking, rows)
                self._dirty = False
                return Availability(ok=True)
            except Exception as exc:
                reason = _model_error_reason(exc)
                logger.warning("Vector cache load failed: %s", reason)
                self._runtime_reason = reason
                return Availability(ok=False, reason=reason)

    def _load_blocking(self, rows: list[dict[str, Any]]) -> None:
        """Deserialize stored bytes into the numpy cache (blocking)."""
        if not rows:
            self._node_ids = []
            self._node_meta = []
            self._vectors = np.empty((0, 0), dtype=np.float32)
            return

        first = np.frombuffer(bytes(rows[0]["vector"]), dtype=np.float32)
        dim = len(first)
        mat = np.empty((len(rows), dim), dtype=np.float32)
        mat[0] = first
        ids: list[str] = [rows[0]["node_id"]]
        meta: list[dict[str, Any]] = [_row_meta(rows[0])]
        for i, r in enumerate(rows[1:], start=1):
            mat[i] = np.frombuffer(bytes(r["vector"]), dtype=np.float32)
            ids.append(r["node_id"])
            meta.append(_row_meta(r))
        self._node_ids = ids
        self._node_meta = meta
        self._vectors = mat

    # ------------------------------------------------------------------
    # Search by query string
    # ------------------------------------------------------------------

    async def search(
        self,
        store: SqliteStore,
        query: str,
        top_k: int,
    ) -> SemanticResponse:
        """Embed *query* and return the top_k most similar graph nodes."""
        avail = await self._ensure_vectors(store)
        if not avail.ok:
            return SemanticResponse(available=False, reason=avail.reason)
        if not self._node_ids:
            return SemanticResponse(available=True, hits=[])

        # Snapshot local references for thread-safety: mark_dirty() may be
        # called from the event loop while _search_blocking runs in a thread.
        vectors = self._vectors
        node_ids = self._node_ids
        node_meta = self._node_meta
        # Reuse the model loaded during build(); load lazily on first search
        # after a service restart (vectors restored from store, model not yet
        # in memory).
        model = self._model
        if model is None:
            try:
                model = await asyncio.to_thread(
                    lambda: model2vec.StaticModel.from_pretrained(MODEL_ID)
                )
                self._model = model
            except Exception as load_exc:
                reason = _model_error_reason(load_exc)
                logger.warning("Model load failed during search: %s", reason)
                self._runtime_reason = reason
                return SemanticResponse(available=False, reason=reason)
        try:
            hits = await asyncio.to_thread(
                _search_blocking,
                query,
                top_k,
                vectors,
                node_ids,
                node_meta,
                model,
            )
        except Exception as exc:
            reason = _model_error_reason(exc)
            logger.warning("Semantic search failed: %s", reason)
            return SemanticResponse(available=False, reason=reason)
        return SemanticResponse(available=True, hits=hits)

    # ------------------------------------------------------------------
    # Find related (by node id)
    # ------------------------------------------------------------------

    async def find_related(
        self,
        store: SqliteStore,
        node_id: str,
        top_k: int,
    ) -> SemanticResponse:
        """Return the top_k graph nodes most similar to *node_id*."""
        avail = await self._ensure_vectors(store)
        if not avail.ok:
            return SemanticResponse(available=False, reason=avail.reason)
        if not self._node_ids:
            return SemanticResponse(available=True, hits=[])

        vectors = self._vectors
        node_ids = self._node_ids
        node_meta = self._node_meta
        try:
            source_idx = node_ids.index(node_id)
        except ValueError:
            return SemanticResponse(
                available=False,
                reason=f"Node {node_id!r} has no stored embedding.",
            )
        try:
            hits = await asyncio.to_thread(
                _find_related_blocking,
                source_idx,
                top_k,
                vectors,
                node_ids,
                node_meta,
            )
        except Exception as exc:
            reason = _model_error_reason(exc)
            logger.warning("find_related failed: %s", reason)
            return SemanticResponse(available=False, reason=reason)
        return SemanticResponse(available=True, hits=hits)

    # ------------------------------------------------------------------
    # Clustering
    # ------------------------------------------------------------------

    async def compute_clusters(
        self, store: SqliteStore
    ) -> ClusterComputation | None:
        """
        Cluster the stored node vectors using HDBSCAN.

        Returns None when there are too few nodes, so the caller can skip
        the cluster phase without treating it as a hard failure.
        """
        avail = await self._ensure_vectors(store)
        if not avail.ok:
            return None
        if not self._node_ids or len(self._node_ids) < _MIN_CLUSTER_SIZE:
            return None
        if len(self._node_ids) > _MAX_CLUSTER_NODES:
            logger.warning(
                "Skipping clustering: %d nodes exceeds cap %d",
                len(self._node_ids),
                _MAX_CLUSTER_NODES,
            )
            return None

        vectors = self._vectors
        node_ids = list(self._node_ids)
        node_meta = list(self._node_meta)
        try:
            return await asyncio.to_thread(
                _cluster_blocking, node_ids, node_meta, vectors
            )
        except Exception as exc:
            logger.warning("Clustering failed (non-fatal): %s", exc)
            return None


# ------------------------------------------------------------------
# Thread-pool worker functions (pure, no self)
# ------------------------------------------------------------------


def _embed_blocking(
    nodes: list[dict[str, Any]],
) -> tuple[Any, list[tuple[str, bytes]]]:
    """Embed *nodes*; return (model, (node_id, float32_bytes) pairs)."""
    model = model2vec.StaticModel.from_pretrained(MODEL_ID)
    texts = [_embedding_text(n) for n in nodes]
    vectors = np.asarray(model.encode(texts), dtype=np.float32)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    unit = vectors / norms
    return model, [(n["id"], unit[i].tobytes()) for i, n in enumerate(nodes)]


def _search_blocking(
    query: str,
    top_k: int,
    vectors: Any,
    node_ids: list[str],
    node_meta: list[dict[str, Any]],
    model: Any,
) -> list[SemanticHit]:
    """Embed *query* and rank *vectors* by cosine similarity (blocking)."""
    qvec = np.asarray(model.encode([query])[0], dtype=np.float32)
    norm = float(np.linalg.norm(qvec)) or 1.0
    qvec /= norm

    scores = vectors @ qvec
    n = min(top_k, len(scores))
    if n == 0:
        return []
    idxs = np.argpartition(scores, -n)[-n:]
    idxs = idxs[np.argsort(scores[idxs])[::-1]]
    return [
        SemanticHit(
            node_id=node_ids[i],
            kind=node_meta[i]["kind"],
            name=node_meta[i]["name"],
            qualified_name=node_meta[i]["qualified_name"],
            file_path=node_meta[i]["file_path"],
            score=float(scores[i]),
        )
        for i in idxs
    ]


def _find_related_blocking(
    source_idx: int,
    top_k: int,
    vectors: Any,
    node_ids: list[str],
    node_meta: list[dict[str, Any]],
) -> list[SemanticHit]:
    """Find top_k nodes most similar to *vectors[source_idx]* (blocking)."""
    qvec = vectors[source_idx]
    scores = vectors @ qvec
    scores = scores.copy()
    # -2.0 is strictly below any cosine similarity (-1.0 floor for unit
    # vectors), guaranteeing the source is the unique minimum even when
    # another node happens to score exactly -1.0 (antipodal embedding).
    scores[source_idx] = -2.0
    n = min(top_k, len(scores) - 1)
    if n <= 0:
        return []
    idxs = np.argpartition(scores, -n)[-n:]
    idxs = idxs[np.argsort(scores[idxs])[::-1]]
    return [
        SemanticHit(
            node_id=node_ids[i],
            kind=node_meta[i]["kind"],
            name=node_meta[i]["name"],
            qualified_name=node_meta[i]["qualified_name"],
            file_path=node_meta[i]["file_path"],
            score=float(scores[i]),
        )
        for i in idxs
    ]


def _cluster_blocking(
    node_ids: list[str],
    node_meta: list[dict[str, Any]],
    vectors: Any,
) -> ClusterComputation:
    """HDBSCAN-cluster *vectors*, assemble labeled cluster rows (blocking)."""
    labels = HDBSCAN(
        min_cluster_size=_MIN_CLUSTER_SIZE,
        metric="euclidean",
        copy=True,
    ).fit_predict(vectors)

    nodes = [
        {
            "id": node_ids[i],
            "qualified_name": node_meta[i]["qualified_name"],
        }
        for i in range(len(node_ids))
    ]
    return _assemble_clusters(nodes, vectors, labels)


# ------------------------------------------------------------------
# Module-level pure helpers (unit-testable without the model)
# ------------------------------------------------------------------


def _row_meta(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": row["kind"],
        "name": row["name"],
        "qualified_name": row["qualified_name"],
        "file_path": row["file_path"],
    }


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
    """Build the text to embed for a node: name + signature + docstring."""
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
    Derive a short cluster label and top terms from member names.

    Tokenises each member's name into identifier sub-tokens, drops generic
    stopwords, and ranks by frequency.  Returns ``(label, terms)`` where the
    label joins the top terms (falling back to ``"misc"`` if nothing
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

    Noise points (label ``-1``) are left unclustered.  Each member's score
    is the cosine similarity to its cluster centroid (vectors are unit-norm,
    so that is just the dot product), giving a tightness signal that orders
    members and lets callers gauge how representative a member is.
    """
    by_label: dict[int, list[int]] = {}
    for idx, raw in enumerate(labels):
        label = int(raw)
        if label < 0:
            continue
        by_label.setdefault(label, []).append(idx)

    clusters: list[dict[str, Any]] = []
    assignments: list[dict[str, Any]] = []
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
