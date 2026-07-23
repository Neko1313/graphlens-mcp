import pytest

from features.search import service as search_service
from shared.common.db.vector import VectorStore
from shared.common.indexing import persist
from shared.common.indexing.embed import EMBED_DIM, encode
from shared.common.setting.const import CODE_COLLECTION

PROJECT = "proj_phantom"


@pytest.mark.integration
@pytest.mark.search
async def test_search_drops_a_vector_without_a_graph_node(
    graph_store,
    vector_store: VectorStore,
    registry,
):
    # Regression (final review): a vector can outlive its graph node after a
    # crash between the vector write and the graph write; such an orphan must
    # not surface as a search hit with a dead resource link.
    # Arrange — an orphan vector with no matching CodeNode.
    await persist.ensure_code_schema(graph_store)
    await vector_store.ensure_collection(CODE_COLLECTION, EMBED_DIM)
    await vector_store.upsert(
        CODE_COLLECTION,
        [
            {
                "id": persist.gid(PROJECT, "ghost"),
                "vector": encode(["def ghost(): return 1"])[0].tolist(),
                "project_id": PROJECT,
                "local_id": "ghost",
                "kind": "function",
                "name": "ghost",
                "file_path": "g.py",
            },
        ],
    )

    # Act
    hits = await search_service.search(
        graph_store,
        vector_store,
        registry,
        "ghost",
        PROJECT,
    )

    # Assert — the phantom is filtered out.
    assert all(hit.id != "ghost" for hit in hits)
