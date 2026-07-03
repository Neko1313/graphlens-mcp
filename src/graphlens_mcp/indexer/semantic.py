"""
Semantic retrieval over the code graph.

Embeds graph *nodes* (function/method/class) directly with the model2vec
static model and stores the float32 vectors in SQLite.  Semantic search does
in-process cosine-similarity over a cached vector matrix — no file chunking,
no external retrieval service, no chunk→node bridge. Each search hit is
already a graph node, so the result pivots straight into
get_callers/get_callees without extra indirection.

model2vec and numpy are required dependencies.  The only graceful-degradation
path that remains is a model-download failure (network outage, HF egress
blocked), which is stored as a sticky reason and reported via
``available=False`` so the caller can surface it without crashing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import model2vec
import numpy as np

if TYPE_CHECKING:
    from graphlens_mcp.store.sqlite_store import SqliteStore

logger = logging.getLogger(__name__)

MODEL_ID = "minishlab/potion-code-16M"

_SIGNATURE_KEYS = ("signature", "sig")
_DOCSTRING_KEYS = ("docstring", "doc", "documentation")

# Embedding-text budget. Many nodes (Go/Rust methods especially) carry no
# signature or docstring in metadata, so embedding only the name is weak — we
# also fold in a bounded snippet of the actual source body, which is the
# strongest signal for search-by-meaning.
_MAX_EMBED_CHARS = 1500
_MAX_BODY_LINES = 12
_MAX_BODY_CHARS = 600
_MAX_DOC_CHARS = 300


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
    :meth:`search` and :meth:`rank` do in-process cosine-similarity over a
    cached vector matrix loaded from the store on first use and reloaded
    after incremental edits via :meth:`mark_dirty`.

    All blocking work (model load, encode) runs off the event loop in a
    thread pool.  When the model is unreachable every method returns a
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
            nodes = await store.get_nodes_for_embedding()
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
    # Rerank a candidate set by query similarity
    # ------------------------------------------------------------------

    async def rank(
        self, store: SqliteStore, query: str, node_ids: list[str]
    ) -> dict[str, float]:
        """
        Return ``{node_id: cosine}`` of each node's vector to *query*.

        Used to make search selective: order a candidate set by relevance to
        the query instead of truncating in FTS order. Nodes without a vector
        (never embedded) are simply absent. Empty on any unavailability so the
        caller falls back to its own ordering.
        """
        avail = await self._ensure_vectors(store)
        if not avail.ok or self._vectors is None or not self._node_ids:
            return {}
        model = self._model
        if model is None:
            try:
                model = await asyncio.to_thread(
                    lambda: model2vec.StaticModel.from_pretrained(MODEL_ID)
                )
                self._model = model
            except Exception:
                return {}
        index = {nid: i for i, nid in enumerate(self._node_ids)}
        wanted = [(nid, index[nid]) for nid in node_ids if nid in index]
        if not wanted:
            return {}
        vectors = self._vectors

        def _blocking() -> dict[str, float]:
            qvec = np.asarray(model.encode([query])[0], dtype=np.float32)
            qvec /= float(np.linalg.norm(qvec)) or 1.0
            return {nid: float(vectors[i] @ qvec) for nid, i in wanted}

        return await asyncio.to_thread(_blocking)


# ------------------------------------------------------------------
# Thread-pool worker functions (pure, no self)
# ------------------------------------------------------------------


def _embed_blocking(
    nodes: list[dict[str, Any]],
) -> tuple[Any, list[tuple[str, bytes]]]:
    """Embed *nodes*; return (model, (node_id, float32_bytes) pairs)."""
    model = model2vec.StaticModel.from_pretrained(MODEL_ID)
    # Per-build file cache: each source file is read once even though many
    # nodes share it. Local to this call so a later rebuild re-reads from disk
    # (never serving stale source after an edit).
    file_cache: dict[str, list[str]] = {}
    texts = [_embedding_text(n, file_cache) for n in nodes]
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


def _node_body(
    node: dict[str, Any], file_cache: dict[str, list[str]]
) -> str | None:
    """Read a bounded snippet of the node's source body (cached per file)."""
    path = node.get("file_path")
    span = node.get("span_json")
    if not path or not span:
        return None
    try:
        start_line, _, end_line, _ = json.loads(span)
    except (ValueError, TypeError):
        return None
    lines = file_cache.get(path)
    if lines is None:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            lines = []
        file_cache[path] = lines
    if not lines:
        return None
    end = min(end_line, start_line + _MAX_BODY_LINES - 1)
    snippet = "".join(lines[start_line - 1 : end]).strip()
    return snippet[:_MAX_BODY_CHARS] or None


def _embedding_text(
    node: dict[str, Any], file_cache: dict[str, list[str]] | None = None
) -> str:
    """
    Build the text to embed for a node.

    Folds together the qualified name (plus its split identifier tokens, which
    help a static model match natural-language queries), the node kind, the
    signature and docstring when present, and a bounded snippet of the actual
    source body — the body is the strongest signal for nodes that carry no
    docstring/signature in metadata.
    """
    qn = str(node.get("qualified_name") or node.get("name") or "")
    kind = str(node.get("kind") or "")
    parts: list[str] = [f"{kind} {qn}".strip()]

    name = str(node.get("name") or "")
    tokens = _split_identifier(name)
    if len(tokens) > 1:
        parts.append(" ".join(tokens))

    sig = _first_meta(node.get("metadata_json"), _SIGNATURE_KEYS)
    if sig:
        parts.append(sig)
    doc = _first_meta(node.get("metadata_json"), _DOCSTRING_KEYS)
    if doc:
        parts.append(doc.strip()[:_MAX_DOC_CHARS])
    if file_cache is not None:
        body = _node_body(node, file_cache)
        if body:
            parts.append(body)

    return "\n".join(p for p in parts if p)[:_MAX_EMBED_CHARS]


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
