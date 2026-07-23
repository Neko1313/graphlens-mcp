import pytest

from entities.commit import CommitInfo
from features.relations import service as relations_service
from shared.common.indexing import index_project_graph, temporal

PROJECT = "proj_hist"

CALLS_HELPER = (
    "def helper():\n    return 1\n\n\ndef caller():\n    return helper()\n"
)
CALLS_NOTHING = (
    "def helper():\n    return 1\n\n\ndef caller():\n    return 2\n"
)


def _project(tmp_path, module_body):
    root = tmp_path / "proj"
    root.mkdir(exist_ok=True)
    (root / "pyproject.toml").write_text("[project]\nname='p'\nversion='0'\n")
    pkg = root / "p"
    pkg.mkdir(exist_ok=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "m.py").write_text(module_body)
    return root


async def _node_id(graph_store, name):
    rows = await graph_store.execute(
        "MATCH (n:CodeNode {project_id: $p}) WHERE n.name = $name "
        "AND n.kind = 'function' RETURN n.local_id AS id LIMIT 1",
        {"p": PROJECT, "name": name},
    )
    return rows[0]["id"] if rows else None


@pytest.mark.integration
@pytest.mark.temporal
async def test_the_pipeline_records_the_call_graph_of_each_commit(
    tmp_path,
    graph_store,
    vector_store,
):
    # End to end: a real analyze of a real edit, through the real pipeline —
    # the point-in-time answer must follow the code, not the current graph.
    # Arrange — commit one calls helper(); commit two stops calling it.
    root = _project(tmp_path, CALLS_HELPER)
    await index_project_graph(
        root,
        PROJECT,
        graph_store,
        vector_store,
        None,
        CommitInfo(ref="main", sha="a" * 40, time_update=1),
    )
    caller = await _node_id(graph_store, "caller")
    assert caller is not None

    _project(tmp_path, CALLS_NOTHING)
    result = await index_project_graph(
        root,
        PROJECT,
        graph_store,
        vector_store,
        None,
        CommitInfo(ref="main", sha="b" * 40, time_update=2),
    )

    # Act
    before = await temporal.resolve_point(graph_store, PROJECT, "main", "1")
    after = await temporal.resolve_point(graph_store, PROJECT, "main", "2")
    assert before is not None
    assert after is not None
    then = await relations_service.get_relations_at(
        graph_store,
        PROJECT,
        before,
        caller,
    )
    now = await relations_service.get_relations_at(
        graph_store,
        PROJECT,
        after,
        caller,
    )

    # Assert — the removed call survives at the commit that had it.
    assert result.changes is not None
    assert result.changes["edge_deleted"] >= 1
    assert then is not None
    assert now is not None
    assert "helper" in {ref.name for ref in then.callees}
    assert "helper" not in {ref.name for ref in now.callees}
