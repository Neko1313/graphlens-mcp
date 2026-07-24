from typing import Annotated, Literal

from mcp_types import ResourceLink, TextContent
from pydantic import Field

from entities import request
from entities.request import SearchParams
from features.search import service
from shared.common.db.graph import get_graph_store
from shared.common.db.registry import get_registry_store, resolve_project
from shared.common.db.vector import get_vector_store

__all__ = ["search"]

_DETAIL_SOURCE_CAP = 1200
# detailed inlines each hit's source; past a handful that is a token firehose
# (a detailed limit=50 returned 34k chars in one call), and nobody reads 50
# sources at once — the rest stay as one-line entries the caller can info().
_DETAIL_INLINE_CAP = 8


def _label(kind: str, signature: str, file_path: str, line: int | None) -> str:
    head = f"{kind} {signature}".strip()
    where = f"{file_path}:{line}" if line else file_path
    return f"{head} · {where}"


async def search(
    query: Annotated[str, Field(description=request.SEARCH_QUERY)],
    project: Annotated[str | None, Field(description=request.PROJECT)] = None,
    limit: Annotated[int, Field(description=request.SEARCH_LIMIT)] = 25,
    path_glob: Annotated[
        str | None,
        Field(description=request.SEARCH_PATH_GLOB),
    ] = None,
    verbosity: Annotated[
        Literal["concise", "detailed"],
        Field(description=request.SEARCH_VERBOSITY),
    ] = "concise",
    exhaustive: Annotated[
        bool,
        Field(description=request.SEARCH_EXHAUSTIVE),
    ] = False,
) -> list[TextContent | ResourceLink]:
    """Find symbols by meaning, name, or literal content across a project.

    This is the entry point — how you locate a symbol you can't name exactly.
    Once you have one, stop searching: ``relations`` answers who calls, uses,
    or implements it, and ``info`` reads its source. Re-phrasing a query is
    almost never the way to enumerate usages; ``relations`` is.

    Blends semantic (embedding), name-substring, and literal content search.
    ``concise`` (the default) lists each hit as ``name · kind signature ·
    path:line · id`` — for "where is X defined" that line IS the answer, so
    read it instead of calling info. ``detailed`` inlines each hit's source;
    ``exhaustive`` lists every in-scope file path instead. Test files are
    excluded unless ``path_glob`` names them.
    """
    params = SearchParams(
        query=query,
        project=project,
        limit=limit,
        path_glob=path_glob,
        verbosity=verbosity,
        exhaustive=exhaustive,
    )
    graph_store = get_graph_store()
    registry = get_registry_store()
    project_id = await resolve_project(registry, params.project)
    hits = await service.search(
        graph_store,
        get_vector_store(),
        registry,
        params.query,
        project_id,
        params.limit,
        params.path_glob,
        exhaustive=params.exhaustive,
    )

    # Say when the default test filter is in play: an agent that gets 0 hits
    # for a symbol only tests use would otherwise re-query blindly.
    scope = (
        " (test files excluded — name them in path_glob to include)"
        if service.excludes_tests(params.path_glob)
        else ""
    )
    blocks: list[TextContent | ResourceLink] = [
        TextContent(
            type="text",
            text=(
                f"{len(hits)} results for {params.query!r} "
                f"in {project_id}{scope}"
            ),
        ),
    ]
    if params.verbosity == "concise":
        # One text block, one line per hit — carrying the id (for info /
        # relations) and the path:line. A resource_link per hit says the same
        # thing but only a client that can *follow* links reads it; the common
        # tool-calling agent sees an opaque handle and spends a round trip on
        # info(id) to learn a file path this line already gave it.
        rows = "\n".join(
            f"{i}. {hit.name} · "
            f"{_label(hit.kind, hit.signature, hit.file_path, hit.line)}"
            + (f" · id={hit.id}" if hit.id else "")
            for i, hit in enumerate(hits, 1)
        )
        if rows:
            blocks.append(TextContent(type="text", text=rows))
        return blocks

    for rank, hit in enumerate(hits):
        label = _label(hit.kind, hit.signature, hit.file_path, hit.line)
        if hit.id and rank < _DETAIL_INLINE_CAP:
            src, _ = await service.get_node_source(
                graph_store,
                registry,
                project_id,
                hit.id,
            )
            blocks.append(
                TextContent(
                    type="text",
                    text=(
                        f"{hit.name} — {label}\n"
                        f"{src[:_DETAIL_SOURCE_CAP]}\n[{hit.uri}]"
                    ),
                ),
            )
        elif hit.id:
            # Past the inline cap: the concise one-liner, still with the id so
            # the caller can info() the ones that matter.
            blocks.append(
                TextContent(
                    type="text",
                    text=f"{hit.name} · {label} · id={hit.id}",
                ),
            )
        else:
            blocks.append(
                ResourceLink(
                    type="resource_link",
                    name=hit.name,
                    uri=hit.uri,
                    description=label,
                    mime_type="text/x-python",
                ),
            )
    return blocks
