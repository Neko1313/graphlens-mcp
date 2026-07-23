from functools import cache
from urllib.parse import urlsplit

from shared.common.db.graph.kuzu import KuzuGraphStore
from shared.common.db.graph.port import GraphStore
from shared.common.setting.getter_setting import get_graph_db
from shared.common.setting.type import GraphDBHost

__all__ = [
    "GraphStore",
    "get_graph_store",
]

_NEO4J_DEFAULT_PORT = 7687


@cache
def get_graph_store() -> GraphStore:
    """Resolve the GraphStore configured via DB__GRAPH.

    Local (no DSN set): embedded Kuzu, opened on disk. Host (Neo4j DSN set): a
    Neo4j server. Both speak Cypher behind the same port (the Neo4j backend
    applies a thin dialect shim), so callers written against this port don't
    change. Neo4j is imported lazily so the ``neo4j`` extra stays optional.
    """
    graph_db = get_graph_db()

    if isinstance(graph_db, GraphDBHost):
        # Lazy: keeps the neo4j extra optional for local-only installs.
        from shared.common.db.graph.neo4j import (  # noqa: PLC0415
            Neo4jGraphStore,
        )

        parts = urlsplit(str(graph_db.dsn))
        port = parts.port or _NEO4J_DEFAULT_PORT
        uri = f"{parts.scheme}://{parts.hostname}:{port}"
        return Neo4jGraphStore.connect(
            uri, parts.username or "neo4j", parts.password or "",
        )

    return KuzuGraphStore.open(graph_db.path)
