from typing import Literal

from pydantic import BaseModel

from features.info import service
from shared.common.db.graph import get_graph_store
from shared.common.db.registry import get_registry_store, resolve_project

__all__ = ["info"]


async def info(
    target: str,
    project: str | None = None,
    mode: Literal["outline", "source"] = "outline",
    limit: int | None = None,
    offset: int = 0,
    file: str = "",
) -> dict[str, object]:
    """Read a symbol's source, or a file's outline/source, from the graph.

    ``target`` is a symbol (node id or name) or a file path. A symbol returns
    its source + signature + metadata; a file returns its outline of symbols
    (``mode=outline``, default) or full source + importers (``mode=source``),
    windowed by ``limit``/``offset`` lines. An ambiguous name returns
    candidates — narrow with ``file``. Pass ``project`` when more than one is
    indexed. Does NOT search by meaning — use ``search`` for that.
    """
    registry = get_registry_store()
    project_id = await resolve_project(registry, project)
    result = await service.info(
        get_graph_store(),
        registry,
        project_id,
        target,
        mode,
        limit,
        offset,
        file,
    )
    if isinstance(result, BaseModel):
        return result.model_dump()
    return {"type": "not_found", "target": target}
