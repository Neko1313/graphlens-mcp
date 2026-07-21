from functools import cache

from shared.common.db.graph.kuzu import KuzuGraphStore
from shared.common.db.graph.port import GraphStore
from shared.common.setting.getter_setting import get_graph_db
from shared.common.setting.type import GraphDBHost

__all__ = [
    "GraphStore",
    "get_graph_store",
]


@cache
def get_graph_store() -> GraphStore:
    """Resolve the GraphStore configured via DB__GRAPH.

    Local (no DSN set): embedded Kuzu, opened on disk. Host (Neo4j DSN set):
    not wired up yet — Kuzu and Neo4j both speak Cypher, so callers written
    against this port carry over once a Neo4jGraphStore lands.
    """
    graph_db = get_graph_db()

    if isinstance(graph_db, GraphDBHost):
        msg = (
            "Neo4j (host) graph backend is not wired up yet; "
            "unset DB__GRAPH to use the local embedded Kuzu database."
        )
        raise NotImplementedError(msg)

    return KuzuGraphStore.open(graph_db.path)
