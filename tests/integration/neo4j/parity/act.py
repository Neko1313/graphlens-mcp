from pathlib import Path
from types import SimpleNamespace

import pytest

from entities.commit import CommitInfo
from shared.common.indexing import persist, temporal

PROJECT = "proj_neo"
ROOT = Path(".")


def _node(local_id, name, kind="function"):
    return SimpleNamespace(
        id=local_id,
        name=name,
        qualified_name=name,
        kind=SimpleNamespace(value=kind),
        file_path="m.py",
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


@pytest.mark.integration
@pytest.mark.neo4j
async def test_neo4j_runs_the_real_persist_and_query_surface(neo4j_store):
    # The whole point of P05: the SAME persist/query code that runs on embedded
    # Kuzu runs on a real Neo4j behind the dialect shim (DDL skipped, rels()
    # unwrapped, toLower/CONTAINS + the state_at aggregation accepted verbatim).
    store = neo4j_store

    # Arrange — real DDL (a no-op on Neo4j) + the real persist helpers.
    await persist.ensure_code_schema(store)
    await persist.persist_nodes(
        store, ROOT, PROJECT,
        [_node("a", "foo"), _node("b", "bar"), _node("c", "baz")],
    )
    await persist.persist_relations(
        store, PROJECT, [_rel("a", "b"), _rel("b", "c")], {"a", "b", "c"},
    )

    # Act / Assert — name search (toLower + CONTAINS).
    hits = await store.execute(
        "MATCH (n:CodeNode {project_id: $p}) "
        "WHERE toLower(n.name) CONTAINS toLower($q) "
        "RETURN n.local_id AS id",
        {"p": PROJECT, "q": "FO"},
    )
    assert {row["id"] for row in hits} == {"a"}

    # variable-length traversal (rels(r) is unwrapped to r for Neo4j).
    reached = await store.execute(
        "MATCH (n:CodeNode {project_id: $p, local_id: 'a'})"
        "-[r:Rel*1..3]->(m:CodeNode) "
        "WHERE all(x IN rels(r) WHERE x.kind = 'calls') "
        "RETURN DISTINCT m.local_id AS id",
        {"p": PROJECT},
    )
    assert {row["id"] for row in reached} == {"b", "c"}

    # the real temporal log + state_at aggregation on Neo4j.
    await temporal.append_versions(
        store, ROOT, PROJECT,
        CommitInfo(ref="main", sha="s1", time_update=1), [_node("x", "x")],
    )
    await temporal.append_versions(
        store, ROOT, PROJECT,
        CommitInfo(ref="main", sha="s2", time_update=2), [],
    )
    live_at_1 = {
        row["node_id"]
        for row in await temporal.state_at(store, PROJECT, "main", 1)
    }
    live_at_2 = {
        row["node_id"]
        for row in await temporal.state_at(store, PROJECT, "main", 2)
    }
    assert live_at_1 == {"x"}
    assert live_at_2 == set()
