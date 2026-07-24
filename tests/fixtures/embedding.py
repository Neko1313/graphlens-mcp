import hashlib

import numpy as np
import pytest

from shared.common.indexing import embed


class _FakeModel:
    """Stands in for the real model2vec download in every test.

    Deterministic and hash-seeded per text — good enough for the tests that
    exercise encode() (they assert on embed/reuse/delete counts and orphan
    filtering, never on semantic similarity), and it means CI can run fully
    offline without a real network call to Hugging Face.
    """

    def encode(self, texts: list[str]) -> np.ndarray:
        return np.stack([_vector(text) for text in texts])


def _vector(text: str) -> np.ndarray:
    digest = hashlib.sha256(text.encode()).digest()
    seed = int.from_bytes(digest[:8], "big")
    rng = np.random.default_rng(seed)
    return rng.standard_normal(embed.EMBED_DIM).astype(np.float32)


@pytest.fixture(autouse=True)
def _offline_embedding_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep encode() off the network: CI runs with HF_HUB_OFFLINE=1 and no
    egress, so the real model2vec download would fail every time it's hit.
    """
    monkeypatch.setattr(embed, "_model", _FakeModel)
