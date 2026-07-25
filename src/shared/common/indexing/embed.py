import functools
import re
from pathlib import Path

import numpy as np
from graphlens import Node
from model2vec import StaticModel

__all__ = ["EMBED_DIM", "EMBED_KINDS", "embed_text", "encode"]

_MODEL_ID = "minishlab/potion-code-16M"
EMBED_DIM = 256
"""Embedding model output dim; the vector collection is created with it."""

EMBED_KINDS = frozenset({"function", "method", "class"})
"""Only these node kinds carry enough meaning to embed for semantic search."""

_MAX_EMBED_CHARS = 1500
_MAX_BODY_LINES = 12
_TOKEN_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")


@functools.cache
def _model() -> StaticModel:
    return StaticModel.from_pretrained(_MODEL_ID)


def _identifier_tokens(name: str) -> str:
    """Split camel/snake/dotted names into lowercase word tokens."""
    cleaned = name.replace(".", " ").replace("_", " ")
    return " ".join(m.lower() for m in _TOKEN_RE.findall(cleaned))


def _source_snippet(root: Path, node: Node) -> str:
    if node.file_path is None or node.span is None:
        return ""
    path = Path(node.file_path)
    if not path.is_absolute():
        path = root / node.file_path
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return ""
    start = max(node.span.start_line - 1, 0)
    end = min(node.span.end_line, start + _MAX_BODY_LINES)
    return "\n".join(lines[start:end])


def embed_text(root: Path, node: Node) -> str:
    """Build the text to embed for a node: identity + signature/doc + body.

    The source body (sliced via the node's span) is the strongest signal when
    the adapter left no signature/docstring in metadata.
    """
    meta = node.metadata
    parts = [
        f"{node.kind.value} {node.qualified_name}",
        _identifier_tokens(node.qualified_name),
        str(meta.get("signature") or meta.get("sig") or ""),
        str(meta.get("docstring") or meta.get("doc") or ""),
        _source_snippet(root, node),
    ]
    return "\n".join(part for part in parts if part)[:_MAX_EMBED_CHARS]


def encode(texts: list[str]) -> np.ndarray:
    """Encode texts into unit-normalized float32 vectors (cosine-ready)."""
    vecs = _model().encode(texts).astype(np.float32)
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vecs / norms
