from dataclasses import dataclass

from shared.common.db.graph.port import GraphStore
from shared.common.db.registry.port import ProjectRegistry
from shared.common.db.vector.port import VectorStore

__all__ = ["AppContext"]


@dataclass(slots=True)
class AppContext:
    """Lifespan-scoped state — reached via `ctx.request_context`."""

    graph_store: GraphStore
    vector_store: VectorStore
    registry: ProjectRegistry
