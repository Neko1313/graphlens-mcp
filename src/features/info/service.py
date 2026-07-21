import json
from typing import Literal

from entities.result import (
    Candidates,
    FileOutline,
    FileSource,
    NodeInfo,
    NodeRef,
    OutlineEntry,
)
from shared.common import source
from shared.common.db.graph import GraphStore
from shared.common.db.registry import ProjectRegistry
from shared.common.indexing import resolve_symbol

__all__ = ["get_file_outline", "get_file_source", "get_node", "info"]

_OUTLINE_KINDS = ("class", "function", "method")

InfoResult = NodeInfo | FileOutline | FileSource | Candidates | None


def _start_line(span_json: str | None) -> int:
    return json.loads(span_json)[0] if span_json else 0


def _module_qn(path: str) -> str:
    """Best-effort module name for a source path (Python: pkg/base.py →
    pkg.base). Used to match import edges that target a module node.
    """
    stem = path.removesuffix(".py").removesuffix("/__init__")
    return stem.replace("/", ".")


async def _importers(
    graph_store: GraphStore,
    project_id: str,
    path: str,
) -> list[str]:
    """Files that import this file (its module, or a symbol defined in it)."""
    rows = await graph_store.execute(
        "MATCH (a:CodeNode)-[r:Rel {kind: 'imports'}]->"
        "(b:CodeNode {project_id: $p}) "
        "WHERE a.project_id = $p AND a.file_path IS NOT NULL "
        "AND (b.file_path = $path OR b.qualified_name = $module) "
        "RETURN DISTINCT a.file_path AS fp ORDER BY fp",
        {"p": project_id, "path": path, "module": _module_qn(path)},
    )
    return [row["fp"] for row in rows]


async def get_node(
    graph_store: GraphStore,
    registry: ProjectRegistry,
    project_id: str,
    node_id: str,
) -> NodeInfo | None:
    """A node's source + signature + metadata, or None if it isn't indexed."""
    rows = await graph_store.execute(
        "MATCH (n:CodeNode {project_id: $p, local_id: $id}) "
        "RETURN n.local_id AS id, n.name AS name, n.qualified_name AS qn, "
        "n.kind AS kind, n.file_path AS file_path, n.span AS span, "
        "n.metadata AS metadata LIMIT 1",
        {"p": project_id, "id": node_id},
    )
    if not rows:
        return None
    node = rows[0]
    src, signature = "", ""
    project = await registry.get(project_id)
    if project is not None:
        src, signature = await source.read_span(
            project.path, node["file_path"], node["span"],
        )
    return NodeInfo(
        id=node["id"],
        name=node["name"],
        qualified_name=node["qn"],
        kind=node["kind"],
        file_path=node["file_path"],
        signature=signature,
        source=src,
        metadata=json.loads(node["metadata"]) if node["metadata"] else {},
    )


async def get_file_source(
    graph_store: GraphStore,
    registry: ProjectRegistry,
    project_id: str,
    path: str,
    offset: int = 0,
    limit: int | None = None,
) -> FileSource | None:
    """A project file's source (optionally a line window) + its importers."""
    project = await registry.get(project_id)
    if project is None:
        return None
    text = await source.read_file(project.path, path)
    if text is None:
        return None
    if offset or limit is not None:
        lines = text.splitlines()
        start = max(offset, 0)
        end = start + limit if limit is not None else len(lines)
        text = "\n".join(lines[start:end])
    importers = await _importers(graph_store, project_id, path)
    return FileSource(file_path=path, source=text, importers=importers)


async def get_file_outline(
    graph_store: GraphStore,
    registry: ProjectRegistry,
    project_id: str,
    path: str,
) -> FileOutline | None:
    """The classes/functions/methods declared in a file, ordered by line.

    None if the project or file is unknown (distinct from a real file that
    simply declares no symbols, which is an empty list).
    """
    project = await registry.get(project_id)
    if project is None or not await source.is_file(project.path, path):
        return None
    rows = await graph_store.execute(
        "MATCH (n:CodeNode {project_id: $p, file_path: $path}) "
        "WHERE n.kind IN $kinds "
        "RETURN n.local_id AS id, n.name AS name, "
        "n.qualified_name AS qn, n.kind AS kind, n.span AS span",
        {"p": project_id, "path": path, "kinds": list(_OUTLINE_KINDS)},
    )
    rows.sort(key=lambda row: _start_line(row["span"]))
    symbols = [
        OutlineEntry(
            id=row["id"],
            name=row["name"],
            qualified_name=row["qn"],
            kind=row["kind"],
            line=_start_line(row["span"]),
        )
        for row in rows
    ]
    importers = await _importers(graph_store, project_id, path)
    return FileOutline(file_path=path, symbols=symbols, importers=importers)


async def info(  # noqa: PLR0913 - info's optional lookup knobs
    graph_store: GraphStore,
    registry: ProjectRegistry,
    project_id: str,
    target: str,
    mode: Literal["outline", "source"] = "outline",
    limit: int | None = None,
    offset: int = 0,
    file: str = "",
) -> InfoResult:
    """Look up a symbol or file. ``target`` is a node id, a symbol name, or a
    file path; ``mode`` chooses outline (default) vs source for a file;
    ``limit``/``offset`` window a file's source; ``file`` disambiguates a name.
    """
    node = await get_node(graph_store, registry, project_id, target)
    if node is not None:
        return node

    project = await registry.get(project_id)
    if project is not None and await source.is_file(project.path, target):
        if mode == "source":
            return await get_file_source(
                graph_store, registry, project_id, target, offset, limit,
            )
        return await get_file_outline(
            graph_store, registry, project_id, target,
        )

    node_id, candidates = await resolve_symbol(
        graph_store, project_id, target, file,
    )
    if node_id is not None:
        return await get_node(graph_store, registry, project_id, node_id)
    if candidates:
        return Candidates(
            candidates=[NodeRef.model_validate(c) for c in candidates],
        )
    return None
