import asyncio
import json
import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath

from entities.result import SearchHit
from shared.common import source
from shared.common.db.graph import GraphStore
from shared.common.db.registry import ProjectRegistry
from shared.common.db.vector import VectorStore
from shared.common.indexing.embed import encode

__all__ = ["get_node_source", "search"]

_MAX_LIMIT = 100
_SAFE_PREFIX = re.compile(r"^[\w./-]*$")
_DEF_KINDS = ("class", "function", "method")
_RG_TIMEOUT_S = 10.0


def _node_uri(project_id: str, node_id: str) -> str:
    return f"graphlens://{project_id}/node/{node_id}"


def _file_uri(project_id: str, file_path: str) -> str:
    return f"graphlens://{project_id}/file/{file_path}"


def _glob_prefix_filter(path_glob: str) -> str | None:
    """A Milvus ``like`` filter for the glob's literal directory prefix."""
    prefix = re.split(r"[*?\[]", path_glob, maxsplit=1)[0]
    prefix = prefix.rsplit("/", 1)[0] + "/" if "/" in prefix else ""
    if not prefix or not _SAFE_PREFIX.match(prefix):
        return None
    return f'file_path like "{prefix}%"'


def _in_scope(file_path: str, path_glob: str | None) -> bool:
    return not path_glob or PurePosixPath(file_path).full_match(path_glob)


async def _all_files(
    graph_store: GraphStore,
    project_id: str,
    path_glob: str | None,
) -> list[SearchHit]:
    """Every indexed file path (no signatures) — the exhaustive enumeration."""
    rows = await graph_store.execute(
        "MATCH (n:CodeNode {project_id: $p}) WHERE n.file_path IS NOT NULL "
        "RETURN DISTINCT n.file_path AS fp ORDER BY fp",
        {"p": project_id},
    )
    hits = []
    for row in rows:
        file_path = row["fp"]
        if not _in_scope(file_path, path_glob):
            continue
        hits.append(
            SearchHit(
                id="",
                name=file_path,
                kind="file",
                file_path=file_path,
                match="file",
                uri=_file_uri(project_id, file_path),
            ),
        )
    return hits


def _run_ripgrep(root: Path, query: str) -> list[tuple[str, int, str]]:
    exe = shutil.which("rg")
    if exe:
        cmd = [exe, "-n", "--no-heading", "-F", "--", query, str(root)]
    else:
        exe = shutil.which("grep")
        if exe is None:
            return []
        cmd = [exe, "-rnIF", "--", query, str(root)]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=_RG_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    out = []
    for line in proc.stdout.splitlines():
        path, _, rest = line.partition(":")
        lineno, _, text = rest.partition(":")
        try:
            rel = str(Path(path).resolve().relative_to(root))
        except ValueError:
            continue
        if lineno.isdigit():
            out.append((rel, int(lineno), text))
    return out


def _enclosing(defs: list[tuple[list[int], dict]], line: int) -> dict | None:
    best: dict | None = None
    best_span = 1 << 30
    for span, row in defs:
        if span[0] <= line <= span[2] and (span[2] - span[0]) < best_span:
            best, best_span = row, span[2] - span[0]
    return best


async def _content_hits(
    graph_store: GraphStore,
    root: Path,
    project_id: str,
    query: str,
    path_glob: str | None,
    cap: int,
) -> list[SearchHit]:
    """Literal substring hits from ripgrep, mapped to the enclosing symbol."""
    raw = await asyncio.to_thread(_run_ripgrep, root, query)
    by_file: dict[str, list[int]] = {}
    for rel, lineno, _ in raw:
        if _in_scope(rel, path_glob):
            by_file.setdefault(rel, []).append(lineno)
    hits: list[SearchHit] = []
    for file_path, lines in by_file.items():
        rows = await graph_store.execute(
            "MATCH (n:CodeNode {project_id: $p, file_path: $f}) "
            "WHERE n.kind IN $kinds AND n.span IS NOT NULL "
            "RETURN n.local_id AS id, n.name AS name, n.kind AS kind, "
            "n.span AS span",
            {"p": project_id, "f": file_path, "kinds": list(_DEF_KINDS)},
        )
        defs = [(json.loads(r["span"]), r) for r in rows]
        for lineno in sorted(lines):
            enclosing = _enclosing(defs, lineno)
            if enclosing is not None:
                hits.append(
                    SearchHit(
                        id=enclosing["id"], name=enclosing["name"],
                        kind=enclosing["kind"], file_path=file_path,
                        match="content", line=lineno,
                        uri=_node_uri(project_id, enclosing["id"]),
                    ),
                )
            else:
                hits.append(
                    SearchHit(
                        id="", name=file_path, kind="file",
                        file_path=file_path, match="content", line=lineno,
                        uri=_file_uri(project_id, file_path),
                    ),
                )
            if len(hits) >= cap:
                return hits
    return hits


async def _signatures(
    graph_store: GraphStore,
    registry: ProjectRegistry,
    project_id: str,
    ids: list[str],
) -> dict[str, str]:
    if not ids:
        return {}
    project = await registry.get(project_id)
    if project is None:
        return {}
    rows = await graph_store.execute(
        "MATCH (n:CodeNode {project_id: $p}) WHERE n.local_id IN $ids "
        "RETURN n.local_id AS id, n.file_path AS fp, n.span AS span",
        {"p": project_id, "ids": ids},
    )
    out: dict[str, str] = {}
    for row in rows:
        _, signature = await source.read_span(
            project.path, row["fp"], row["span"],
        )
        out[row["id"]] = signature
    return out


async def search(  # noqa: PLR0913, PLR0912 - query tool knobs + blend passes
    graph_store: GraphStore,
    vector_store: VectorStore,
    registry: ProjectRegistry,
    query: str,
    project_id: str,
    limit: int = 25,
    path_glob: str | None = None,
    *,
    exhaustive: bool = False,
) -> list[SearchHit]:
    """Semantic + name + content hits (each with a signature). ``exhaustive``
    returns every in-scope file path instead.
    """
    query = query.strip()
    if not query:
        return []
    limit = max(1, min(limit, _MAX_LIMIT))
    if exhaustive:
        return await _all_files(graph_store, project_id, path_glob)

    root = None
    project = await registry.get(project_id)
    if project is not None:
        root = project.path

    hits: list[SearchHit] = []
    seen: set[str] = set()

    filter_expr = _glob_prefix_filter(path_glob) if path_glob else None
    over_fetch = limit if filter_expr or not path_glob else limit * 10
    raw = await vector_store.search(
        project_id, encode([query])[0].tolist(),
        limit=max(over_fetch, limit),
        filter_expr=filter_expr,
        output_fields=["name", "kind", "file_path"],
    )
    for hit in raw:
        entity = hit.get("entity", {})
        file_path = entity.get("file_path") or ""
        node_id = hit["id"]
        if node_id in seen or not _in_scope(file_path, path_glob):
            continue
        seen.add(node_id)
        hits.append(
            SearchHit(
                id=node_id, name=entity.get("name") or "",
                kind=entity.get("kind") or "", file_path=file_path,
                score=round(float(hit.get("distance", 0.0)), 4),
                match="semantic", uri=_node_uri(project_id, node_id),
            ),
        )
        if len(hits) >= limit:
            break

    if len(hits) < limit:
        rows = await graph_store.execute(
            "MATCH (n:CodeNode {project_id: $p}) "
            "WHERE contains(lower(n.name), lower($q)) "
            "RETURN n.local_id AS id, n.name AS name, n.kind AS kind, "
            "n.file_path AS file_path LIMIT $lim",
            {"p": project_id, "q": query, "lim": (limit - len(hits)) * 3},
        )
        for row in rows:
            node_id = row["id"]
            file_path = row["file_path"] or ""
            if node_id in seen or not _in_scope(file_path, path_glob):
                continue
            seen.add(node_id)
            hits.append(
                SearchHit(
                    id=node_id, name=row["name"], kind=row["kind"],
                    file_path=file_path, match="name",
                    uri=_node_uri(project_id, node_id),
                ),
            )
            if len(hits) >= limit:
                break

    if len(hits) < limit and root is not None:
        for hit in await _content_hits(
            graph_store, root, project_id, query, path_glob, limit - len(hits),
        ):
            key = hit.id or f"{hit.file_path}:{hit.line}"
            if key in seen:
                continue
            seen.add(key)
            hits.append(hit)
            if len(hits) >= limit:
                break

    signatures = await _signatures(
        graph_store, registry, project_id, [h.id for h in hits if h.id],
    )
    for hit in hits:
        hit.signature = signatures.get(hit.id, "")
    return hits


async def get_node_source(
    graph_store: GraphStore,
    registry: ProjectRegistry,
    project_id: str,
    node_id: str,
) -> tuple[str, str]:
    """``(source, signature)`` for a hit, for detailed search output."""
    rows = await graph_store.execute(
        "MATCH (n:CodeNode {project_id: $p, local_id: $id}) "
        "RETURN n.file_path AS file_path, n.span AS span LIMIT 1",
        {"p": project_id, "id": node_id},
    )
    if not rows:
        return "", ""
    project = await registry.get(project_id)
    if project is None:
        return "", ""
    return await source.read_span(
        project.path, rows[0]["file_path"], rows[0]["span"],
    )
