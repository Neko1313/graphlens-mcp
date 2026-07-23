from collections.abc import AsyncGenerator, Iterator

import pytest
import pytest_asyncio

from shared.common.db.vector.milvus import MilvusVectorStore
from shared.common.setting.const import CODE_COLLECTION


@pytest.fixture(scope="session")
def _milvus_session(tmp_path_factory) -> Iterator[MilvusVectorStore]:
    """One Milvus Lite instance for the whole session.

    Milvus Lite starts a local server per client and doesn't tolerate many
    instances in one process, so a single shared instance is reused; the
    per-test ``vector_store`` fixture drops the collection to keep tests
    isolated. A sync fixture avoids a session-scoped event loop.
    """
    path = tmp_path_factory.mktemp("milvus") / "vectors.db"
    store = MilvusVectorStore.open(str(path))
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
