import pytest

from features.search import service as search_service
from shared.common.db.vector import VectorStore
from shared.common.indexing import persist
from shared.common.indexing.embed import EMBED_DIM
from shared.common.setting.const import CODE_COLLECTION

PROJECT = "proj_noise_kinds"

# One name, four nodes sharing it: the real function plus the phantom and import
# nodes graphlens emits around a re-exported symbol. The phantom is a dead-end
# id and must never surface; the imports mark files that use the symbol, so they
# stay — an impact question ("which files use parseBody") depends on them.
NODES = [
    ("real", "parseBody", "function", "utils/body.ts"),
    ("phantom", "parseBody", "external_symbol", "utils/body.ts"),
    ("imp1", "parseBody", "import", "router.ts"),
    ("imp2", "parseBody", "import", "server.ts"),
]


async def _seed(graph_store, vector_store: VectorStore) -> None:
    await persist.ensure_code_schema(graph_store)
    await vector_store.ensure_collection(CODE_COLLECTION, EMBED_DIM)
    for local_id, name, kind, file_path in NODES:
        await graph_store.execute(
            "CREATE (n:CodeNode {id: $gid, project_id: $p, local_id: $lid, "
            "name: $name, kind: $kind, file_path: $fp, span: '1-2'})",
            {
                "gid": persist.gid(PROJECT, local_id),
                "p": PROJECT,
                "lid": local_id,
                "name": name,
                "kind": kind,
                "fp": file_path,
            },
        )


@pytest.mark.integration
@pytest.mark.search
async def test_name_search_drops_the_phantom_but_keeps_imports(
    graph_store,
    vector_store: VectorStore,
    registry,
):
    # The bug this guards: the name-match pass ranked the external_symbol phantom
    # first, so a weak model handed its id looped navigating a dead end. The
    # phantom must be gone; the definition and the import sites (enumeration
    # signal) must remain.
    await _seed(graph_store, vector_store)

    hits = await search_service.search(
        graph_store,
        vector_store,
        registry,
        "parseBody",
        PROJECT,
    )

    ids = {hit.id for hit in hits}
    assert "phantom" not in ids
    assert "real" in ids
    assert {"imp1", "imp2"} <= ids
    assert all(hit.kind != "external_symbol" for hit in hits)
