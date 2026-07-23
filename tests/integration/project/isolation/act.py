from types import SimpleNamespace

import pytest

from shared.common.indexing import persist


def _node(local_id, kind="function", file_path="m.py"):
    return SimpleNamespace(
        id=local_id,
        name=local_id,
        qualified_name=local_id,
        kind=SimpleNamespace(value=kind),
        file_path=file_path,
        span=None,
        metadata={},
    )


def _rel(source, target, kind="calls"):
    return SimpleNamespace(
        source_id=source,
        target_id=target,
        kind=SimpleNamespace(value=kind),
        metadata={},
    )


async def _seed_chain(store, root, project_id):
    # Same local ids and a->b->c chain in every project, so only the
    # project-namespaced gid ({project_id}::{local_id}) keeps them apart.
    nodes = [_node("a"), _node("b"), _node("c")]
    relations = [_rel("a", "b"), _rel("b", "c")]
    await persist.persist_nodes(store, root, project_id, nodes)
    await persist.persist_relations(
        store,
        project_id,
        relations,
        {"a", "b", "c"},
    )


@pytest.mark.integration
@pytest.mark.isolation
async def test_multihop_traversal_never_crosses_into_another_project(
    graph_store,
    tmp_path,
):
    # Arrange — two projects sharing identical local ids and edge shape.
    await _seed_chain(graph_store, tmp_path, "proja_11111111")
    await _seed_chain(graph_store, tmp_path, "projb_22222222")

    # Act — walk up to 3 hops out of project A's "a".
    rows = await graph_store.execute(
        "MATCH (n:CodeNode {project_id: $p, local_id: 'a'})"
        "-[r:Rel*1..3]->(m:CodeNode) "
        "RETURN DISTINCT m.project_id AS pid, m.local_id AS id",
        {"p": "proja_11111111"},
    )

    # Assert — every reached node is in project A; exactly b and c are seen.
    assert {row["pid"] for row in rows} == {"proja_11111111"}
    assert {row["id"] for row in rows} == {"b", "c"}
