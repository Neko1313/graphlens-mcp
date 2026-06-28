"""Unit tests for SqliteStore: persistence, FTS, graph CTEs and pruning."""

from __future__ import annotations

import pytest
from graphlens import NodeKind, RelationKind

from tests.conftest import graph_of, make_node, make_relation

pytestmark = [pytest.mark.unit, pytest.mark.store]

FILE = "/proj/m.py"
META = ("hash", 1.0, 10, "ok", "python")


async def _apply(store, nodes, relations, file_path=FILE):
    await store.apply_patch(graph_of(nodes, relations), file_path, *META)


async def test_apply_patch_persists_node_and_get_node_returns_it(store):
    # Arrange
    helper = make_node("pkg.helper", file_path=FILE)
    # Act
    await _apply(store, [helper], [])
    # Assert
    row = await store.get_node(helper.id)
    assert row is not None
    assert row["qualified_name"] == "pkg.helper"
    assert row["file_path"] == FILE
    assert await store.node_count() == 1


async def test_search_symbols_finds_node_by_name(store):
    # Arrange
    await _apply(store, [make_node("pkg.create_order", file_path=FILE)], [])
    # Act
    hits = await store.search_symbols("create_order")
    # Assert
    assert [h["name"] for h in hits] == ["create_order"]


async def test_get_callees_follows_calls_up_to_depth(store):
    # Arrange: a -> b -> c
    a, b, c = (make_node(n, file_path=FILE) for n in ("m.a", "m.b", "m.c"))
    rels = [
        make_relation(a, b, RelationKind.CALLS),
        make_relation(b, c, RelationKind.CALLS),
    ]
    await _apply(store, [a, b, c], rels)
    # Act
    deep = {n["name"] for n in await store.get_callees(a.id, max_depth=3)}
    shallow = {n["name"] for n in await store.get_callees(a.id, max_depth=1)}
    # Assert
    assert deep == {"b", "c"}
    assert shallow == {"b"}


async def test_get_callers_is_the_mirror_of_callees(store):
    # Arrange: a -> b -> c
    a, b, c = (make_node(n, file_path=FILE) for n in ("m.a", "m.b", "m.c"))
    rels = [
        make_relation(a, b, RelationKind.CALLS),
        make_relation(b, c, RelationKind.CALLS),
    ]
    await _apply(store, [a, b, c], rels)
    # Act
    callers = {n["name"] for n in await store.get_callers(c.id, max_depth=3)}
    # Assert
    assert callers == {"a", "b"}


async def test_call_graph_cte_is_cycle_safe(store):
    # Arrange: a -> b -> a (a cycle that must not loop forever)
    a, b = make_node("m.a", file_path=FILE), make_node("m.b", file_path=FILE)
    rels = [
        make_relation(a, b, RelationKind.CALLS),
        make_relation(b, a, RelationKind.CALLS),
    ]
    await _apply(store, [a, b], rels)
    # Act
    callees = {n["name"] for n in await store.get_callees(a.id, max_depth=10)}
    # Assert: terminates and excludes the start node
    assert callees == {"b"}


async def test_reindexing_a_file_replaces_its_nodes(store):
    # Arrange: first index has two functions
    old = [
        make_node("m.gone", file_path=FILE),
        make_node("m.kept", file_path=FILE),
    ]
    await _apply(store, old, [])
    # Act: re-index the same file with only one function
    await _apply(store, [make_node("m.kept", file_path=FILE)], [])
    # Assert: the dropped symbol is gone, no duplicates
    names = {n["name"] for n in await store.get_nodes_in_file(FILE)}
    assert names == {"kept"}


async def test_delete_file_prunes_nodes_edges_and_search(store):
    # Arrange
    a, b = make_node("m.a", file_path=FILE), make_node("m.b", file_path=FILE)
    await _apply(store, [a, b], [make_relation(a, b, RelationKind.CALLS)])
    # Act
    removed = await store.delete_file(FILE)
    # Assert
    assert removed is True
    assert await store.node_count() == 0
    assert await store.edge_count() == 0
    assert await store.search_symbols("a") == []
    assert await store.get_file_info(FILE) is None


async def test_reindex_preserves_synthesized_cross_language_edges(store):
    # Arrange: a file's function plus a synthesized COMMUNICATES_WITH edge from it
    handler = make_node("svc.handler", file_path=FILE)
    await _apply(store, [handler], [])
    await store.apply_cross_language_edges(
        [
            (
                handler.id,
                "other-service-node",
                RelationKind.COMMUNICATES_WITH.value,
            )
        ]
    )
    # Act: re-index the same file (incremental) — single-file analysis never re-emits it
    await _apply(store, [handler], [])
    # Assert: the cross-language edge survived the per-file delete
    async with store._conn.execute(
        "SELECT COUNT(*) FROM edges WHERE kind = 'communicates_with'"
    ) as cur:
        surviving = (await cur.fetchone())[0]
    assert surviving == 1


async def test_apply_patch_persists_only_file_owned_nodes(store):
    # Arrange: a graph for FILE that also carries a foreign edge-target node (as
    # subgraph_for_file / single-file analyze surface). Only the owned node is ours.
    owned = make_node("m.local", file_path=FILE)
    foreign = make_node("other.remote", file_path="/proj/other.py")
    rels = [make_relation(owned, foreign, RelationKind.CALLS)]
    # Act: patch FILE with a graph containing both nodes
    await _apply(store, [owned, foreign], rels)
    # Assert: the foreign node is NOT written by this file's patch (its own file owns it)
    assert await store.get_node(owned.id) is not None
    assert await store.get_node(foreign.id) is None
    # the edge is still recorded; its dangling target is filtered at read time
    assert await store.edge_count() == 1


async def test_failed_write_rolls_back_and_leaves_no_partial_state(store):
    # Arrange: a clean store
    assert await store.node_count() == 0
    # Act: a write that inserts a node then raises before commit must roll back
    boom = make_node("m.boom", file_path=FILE)

    async def _failing_write() -> None:
        async with store._writing():
            await store._conn.execute(
                "INSERT INTO nodes(id, kind, qualified_name, name) VALUES(?, ?, ?, ?)",
                (boom.id, "function", boom.qualified_name, boom.name),
            )
            msg = "boom"
            raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="boom"):
        await _failing_write()
    # Assert: nothing persisted — the partial insert was rolled back, not committed
    assert await store.node_count() == 0


async def test_worst_status_for_files_reports_least_complete(store):
    # Arrange: two files indexed at different graph qualities
    await store.apply_patch(
        graph_of([make_node("a.x", file_path="/a.py")], []),
        "/a.py",
        "h",
        1.0,
        1,
        "ok",
        "python",
    )
    await store.apply_patch(
        graph_of([make_node("b.y", file_path="/b.py")], []),
        "/b.py",
        "h",
        1.0,
        1,
        "degraded",
        "python",
    )
    # Act / Assert: the aggregate is the least-complete of the two
    assert (
        await store.get_worst_status_for_files(["/a.py", "/b.py"])
        == "degraded"
    )
    assert await store.get_worst_status_for_files(["/a.py"]) == "ok"


async def test_imported_paths_round_trip(store):
    # Arrange: an IMPORTS edge from b.py into a.py records a dep
    helper = make_node("a.helper", file_path="/a.py")
    importer = make_node("b.use", file_path="/b.py")
    await store.apply_patch(
        graph_of([helper], []), "/a.py", "h", 1.0, 1, "ok", "python"
    )
    await store.apply_patch(
        graph_of(
            [importer, helper],
            [make_relation(importer, helper, RelationKind.IMPORTS)],
        ),
        "/b.py",
        "h",
        1.0,
        1,
        "ok",
        "python",
    )
    # Act / Assert
    assert await store.get_imported_paths("/b.py") == ["/a.py"]


async def test_imported_files_escapes_underscore_in_module_name(store):
    # b imports the fileless MODULE node `pkg.sub_mod`. A sibling file whose
    # module is `pkg.subXmod` must NOT match — the '_' is a literal, not a LIKE
    # single-char wildcard.
    use = make_node("b.use", file_path="/b.py")
    mod = make_node("pkg.sub_mod", kind=NodeKind.MODULE, file_path=None)
    real = make_node("pkg.sub_mod.helper", file_path="/sub_mod.py")
    sibling = make_node("pkg.subXmod.foo", file_path="/subXmod.py")
    await store.apply_patch(
        graph_of([real], []), "/sub_mod.py", "h", 1.0, 1, "ok", "python"
    )
    await store.apply_patch(
        graph_of([sibling], []), "/subXmod.py", "h", 1.0, 1, "ok", "python"
    )
    await store.apply_patch(
        graph_of([use], [make_relation(use, mod, RelationKind.IMPORTS)]),
        "/b.py",
        "h",
        1.0,
        1,
        "ok",
        "python",
    )
    await store.apply_structural(graph_of([mod], []))
    # Act
    deps = set(await store.get_imported_files("/b.py"))
    importers = set(await store.get_importer_files("/sub_mod.py"))
    # Assert: only the real module file matches, not the `_`-wildcard sibling
    assert deps == {"/sub_mod.py"}
    assert "/subXmod.py" not in deps
    assert importers == {"/b.py"}


async def _count_communicates(store) -> int:
    async with store._conn.execute(
        "SELECT COUNT(*) FROM edges WHERE kind = 'communicates_with'"
    ) as cur:
        return (await cur.fetchone())[0]


async def test_get_boundary_ports_collects_all_participants(store):
    # server.py exposes a boundary; client.py consumes it. Asking for the
    # boundary ports of *just* client.py must still return the server port, so
    # a re-synthesis pass can rebuild the complete pairwise edge set.
    server = make_node("svc.serve", file_path="/server.py")
    client = make_node("web.call", file_path="/client.py")
    boundary = make_node("http:GET /x", kind=NodeKind.BOUNDARY, file_path=None)
    await _apply(
        store,
        [server, boundary],
        [make_relation(server, boundary, RelationKind.EXPOSES)],
        file_path="/server.py",
    )
    await _apply(
        store,
        [client, boundary],
        [make_relation(client, boundary, RelationKind.CONSUMES)],
        file_path="/client.py",
    )
    await store.apply_structural(graph_of([boundary], []))

    ports = await store.get_boundary_ports_for_files(["/client.py"])
    assert set(ports) == {boundary.id}
    assert set(ports[boundary.id]) == {
        (server.id, "exposes"),
        (client.id, "consumes"),
    }


async def test_resynthesize_links_a_newly_added_consumer(store):
    # A new consumer added incrementally gets no COMMUNICATES_WITH from the
    # per-file patch (synthesis is a full-index pass). Re-synthesizing the
    # affected boundary links it without a full reindex.
    server = make_node("svc.serve", file_path="/server.py")
    boundary = make_node("http:GET /x", kind=NodeKind.BOUNDARY, file_path=None)
    await _apply(
        store,
        [server, boundary],
        [make_relation(server, boundary, RelationKind.EXPOSES)],
        file_path="/server.py",
    )
    await store.apply_structural(graph_of([boundary], []))
    assert await _count_communicates(store) == 0  # no consumer yet

    client = make_node("web.call", file_path="/client.py")
    await _apply(
        store,
        [client, boundary],
        [make_relation(client, boundary, RelationKind.CONSUMES)],
        file_path="/client.py",
    )
    ports = await store.get_boundary_ports_for_files(["/client.py"])
    participants = {nid for plist in ports.values() for nid, _ in plist}
    await store.resynthesize_cross_language(
        [
            (server.id, client.id, RelationKind.COMMUNICATES_WITH.value),
            (client.id, server.id, RelationKind.COMMUNICATES_WITH.value),
        ],
        participants,
    )

    linked = {
        n["name"] for n in await store.get_cross_language_calls(server.id)
    }
    assert "call" in linked
    assert await _count_communicates(store) == 2  # both directions, deduped


async def test_resynthesize_drops_dangling_cross_language_edges(store):
    # A renamed exposer leaves its old COMMUNICATES_WITH edge pointing at a
    # node that no longer exists; the re-synthesis pass must prune it.
    server = make_node("svc.serve", file_path="/server.py")
    await _apply(store, [server], [], file_path="/server.py")
    await store.apply_cross_language_edges(
        [(server.id, "ghost-node", RelationKind.COMMUNICATES_WITH.value)]
    )
    assert await _count_communicates(store) == 1

    # 'ghost-node' is not a real node, so the edge is dangling and pruned.
    await store.resynthesize_cross_language([], set())
    assert await _count_communicates(store) == 0


async def test_cross_language_calls_resolve_through_a_shared_boundary(store):
    # Arrange: exposer (server) and consumer (client) meet at one HTTP boundary
    server = make_node("svc.get_user", file_path="/svc.py")
    client = make_node("web.fetch_user", file_path="/svc.py")
    boundary = make_node(
        "http:GET /users/{}", kind=NodeKind.BOUNDARY, file_path=None
    )
    rels = [
        make_relation(server, boundary, RelationKind.EXPOSES),
        make_relation(client, boundary, RelationKind.CONSUMES),
    ]
    # File-owned nodes + their EXPOSES/CONSUMES edges go through apply_patch; the
    # fileless boundary node is persisted by apply_structural (as the full index does).
    await _apply(store, [server, client, boundary], rels, file_path="/svc.py")
    await store.apply_structural(graph_of([server, client, boundary], rels))
    # Act
    linked = {
        n["name"] for n in await store.get_cross_language_calls(server.id)
    }
    # Assert
    assert "fetch_user" in linked
