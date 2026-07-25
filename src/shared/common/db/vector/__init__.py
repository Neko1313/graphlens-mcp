from functools import cache

from shared.common.db.vector.milvus import MilvusVectorStore
from shared.common.db.vector.port import VectorStore
from shared.common.setting.getter_setting import get_vector_db
from shared.common.setting.type import VectorDBHost

__all__ = [
    "VectorStore",
    "get_vector_store",
]


@cache
def get_vector_store() -> VectorStore:
    """Resolve the VectorStore configured via DB__VECTOR.

    Local (no DSN set): Milvus Lite, opened on disk. Host (DSN set): a full
    Milvus deployment. Same client either way — only the URI changes.
    """
    vector_db = get_vector_db()

    if isinstance(vector_db, VectorDBHost):
        return MilvusVectorStore.open(str(vector_db.dsn))

    return MilvusVectorStore.open(str(vector_db.path))
