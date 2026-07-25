import pytest

from features.relations import service as relations_service
from shared.common.indexing import persist

PROJECT = "proj_coverage"


async def _seed(graph_store) -> None:
    # A caller and a callee joined by the only edge kind the Rust analyzer
    # produces: `calls`. No inherits_from, no references — exactly the shape
    # ripgrep indexes as.
    await persist.ensure_code_schema(graph_store)
    for local_id, name in (("caller", "run"), ("callee", "search")):
        await graph_store.execute(
            "CREATE (n:CodeNode {id: $gid, project_id: $p, local_id: $lid, "
            "name: $name, qualified_name: $name, kind: 'function', "
            "file_path: 'main.rs', span: '1-2'})",
            {
                "gid": persist.gid(PROJECT, local_id),
                "p": PROJECT,
                "lid": local_id,
                "name": name,
            },
        )
    await graph_store.execute(
        "MATCH (a:CodeNode {id: $a}), (b:CodeNode {id: $b}) "
        "CREATE (a)-[:Rel {kind: 'calls', metadata: ''}]->(b)",
        {
            "a": persist.gid(PROJECT, "caller"),
            "b": persist.gid(PROJECT, "callee"),
        },
    )


@pytest.mark.integration
@pytest.mark.relations
async def test_groups_the_analyzer_never_produces_are_reported_as_unknown(
    graph_store,
):
    # An empty `implementors` must not read as "nothing implements this": for
    # Rust the analyzer emits no inherits_from at all, and an agent that
    # believes the emptiness re-derives the answer by hand for 20+ calls.
    await _seed(graph_store)

    result = await relations_service.get_relations(
        graph_store,
        PROJECT,
        "callee",
    )

    assert result is not None
    assert result.callers_total == 1
    assert set(result.not_indexed) == {"implementors", "references"}


@pytest.mark.integration
@pytest.mark.relations
async def test_a_group_with_edges_is_not_called_unknown(graph_store):
    # The counterpart: once an edge kind exists in the project, its group is
    # answerable and must not carry the caveat.
    await _seed(graph_store)
    await graph_store.execute(
        "MATCH (a:CodeNode {id: $a}), (b:CodeNode {id: $b}) "
        "CREATE (a)-[:Rel {kind: 'references', metadata: ''}]->(b)",
        {
            "a": persist.gid(PROJECT, "caller"),
            "b": persist.gid(PROJECT, "callee"),
        },
    )

    result = await relations_service.get_relations(
        graph_store,
        PROJECT,
        "callee",
    )

    assert result is not None
    assert result.not_indexed == ["implementors"]
