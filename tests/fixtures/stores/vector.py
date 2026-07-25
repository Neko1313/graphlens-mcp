import time
from collections.abc import AsyncGenerator, Iterator

import pytest
import pytest_asyncio

from shared.common.db.vector.milvus import MilvusVectorStore
from shared.common.setting.const import CODE_COLLECTION

_START_ATTEMPTS = 4
_START_BACKOFF = 1.5


def _open_milvus(tmp_path_factory) -> MilvusVectorStore:
    """Start Milvus Lite, retrying a slow startup on a fresh database path.

    Milvus Lite spawns a local server and connects to it over a unix socket;
    under load (a parallel run keeps every core busy analyzing) the server can
    miss the client's connect window, which surfaces as a socket that isn't
    there yet. A retry on a clean path clears it — a half-started attempt
    leaves a lock behind, so reusing the path would just fail again.
    """
    last: Exception | None = None
    for attempt in range(_START_ATTEMPTS):
        path = tmp_path_factory.mktemp(f"milvus{attempt}") / "vectors.db"
        try:
            return MilvusVectorStore.open(str(path))
        except Exception as exc:
            last = exc
            time.sleep(_START_BACKOFF * (attempt + 1))
    msg = f"Milvus Lite did not start after {_START_ATTEMPTS} attempts"
    raise RuntimeError(msg) from last


@pytest.fixture(scope="session")
def _milvus_session(tmp_path_factory) -> Iterator[MilvusVectorStore]:
    """One Milvus Lite instance for the whole session.

    Milvus Lite starts a local server per client and doesn't tolerate many
    instances at once, so a single shared instance is reused; the per-test
    ``vector_store`` fixture drops the collection to keep tests isolated. Under
    ``-n`` that would be one instance per worker, so ``tests/conftest.py`` pins
    every test that needs it to one xdist worker. A sync fixture avoids a
    session-scoped event loop.
    """
    store = _open_milvus(tmp_path_factory)
    yield store
    store._client.close()


@pytest_asyncio.fixture
async def vector_store(
    _milvus_session: MilvusVectorStore,
) -> AsyncGenerator[MilvusVectorStore]:
    """The shared Milvus Lite store, with the code collection cleared after."""
    try:
        yield _milvus_session
    finally:
        await _milvus_session.drop_collection(CODE_COLLECTION)
