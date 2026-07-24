import pytest

from features.search import service as search_service
from shared.common.db.vector import VectorStore
from shared.common.indexing import persist
from shared.common.indexing.embed import EMBED_DIM, encode
from shared.common.setting.const import CODE_COLLECTION

PROJECT = "proj_test_scope"

# One symbol name, three files: production code, a Go-style sibling test, and a
# test package. Searching the name must reach only the first by default.
NODES = [
    ("prod", "handle", "server.go"),
    ("sibling_test", "handleTest", "server_test.go"),
    ("pkg_test", "handleCase", "tests/cases.go"),
]


async def _seed(graph_store, vector_store: VectorStore) -> None:
    await persist.ensure_code_schema(graph_store)
    await vector_store.ensure_collection(CODE_COLLECTION, EMBED_DIM)
    for local_id, name, file_path in NODES:
        await graph_store.execute(
            "CREATE (n:CodeNode {id: $gid, project_id: $p, local_id: $lid, "
            "name: $name, kind: 'function', file_path: $fp, span: '1-2'})",
            {
                "gid": persist.gid(PROJECT, local_id),
                "p": PROJECT,
                "lid": local_id,
                "name": name,
                "fp": file_path,
            },
        )
        await vector_store.upsert(
            CODE_COLLECTION,
            [
                {
                    "id": persist.gid(PROJECT, local_id),
                    "vector": encode([f"func {name}()"])[0].tolist(),
                    "project_id": PROJECT,
                    "local_id": local_id,
                    "kind": "function",
                    "name": name,
                    "file_path": file_path,
                },
            ],
        )


@pytest.mark.integration
@pytest.mark.search
async def test_search_excludes_test_files_by_default(
    graph_store,
    vector_store: VectorStore,
    registry,
):
    # The tool documents that test code is filtered out unless the glob asks
    # for it — a codebase's tests otherwise crowd out the symbol being sought.
    await _seed(graph_store, vector_store)

    hits = await search_service.search(
        graph_store,
        vector_store,
        registry,
        "handle",
        PROJECT,
    )

    assert {hit.file_path for hit in hits} == {"server.go"}


@pytest.mark.integration
@pytest.mark.search
async def test_a_glob_naming_tests_opts_back_in(
    graph_store,
    vector_store: VectorStore,
    registry,
):
    # The escape hatch: asking for tests explicitly must return them, or
    # "who uses this, including from tests" becomes unanswerable.
    await _seed(graph_store, vector_store)

    hits = await search_service.search(
        graph_store,
        vector_store,
        registry,
        "handle",
        PROJECT,
        path_glob="**/*_test.go",
    )

    assert {hit.file_path for hit in hits} == {"server_test.go"}
