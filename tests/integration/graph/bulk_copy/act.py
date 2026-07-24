import pytest

from shared.common.db.graph.port import GraphStoreError, SupportsBulkCopy
from shared.common.indexing import persist


@pytest.mark.integration
@pytest.mark.indexing
async def test_the_kuzu_store_advertises_bulk_copy(graph_store):
    # persist branches on this capability, so the embedded backend must carry
    # it — that is the whole point of the O(n²)-avoiding write path.
    assert isinstance(graph_store, SupportsBulkCopy)


@pytest.mark.integration
@pytest.mark.indexing
async def test_bulk_copy_round_trips_nasty_metadata_exactly(graph_store):
    # The reason plain CSV was abandoned: metadata is JSON that carries commas,
    # quotes and (escaped) newlines. A control-char delimiter needs no quoting,
    # so every byte must survive unchanged.
    await persist.ensure_code_schema(graph_store)
    nasty = '{"span": "Span(a=1, b=2)", "q": "he said \\"hi\\"", "nl": "x\\ny"}'
    await graph_store.bulk_copy(
        "CodeNode",
        [
            ("p::a", "p", "a", "function", "a", "a", "f.py", "[1,2]", "{}", "h1"),
            ("p::b", "p", "b", "function", "b", "b", "f.py", "[3,4]", nasty, "h2"),
        ],
    )
    await graph_store.bulk_copy("Rel", [("p::a", "p::b", "calls", nasty)])

    rows = await graph_store.execute(
        "MATCH (a:CodeNode {id: 'p::a'})-[e:Rel]->(b:CodeNode) "
        "RETURN e.metadata AS m, b.metadata AS bm",
    )
    assert rows[0]["m"] == nasty
    assert rows[0]["bm"] == nasty


@pytest.mark.integration
@pytest.mark.indexing
async def test_bulk_copy_appends_without_touching_other_projects(graph_store):
    # The node/edge tables are shared across projects; a COPY for one project
    # must never disturb another's rows.
    await persist.ensure_code_schema(graph_store)
    await graph_store.bulk_copy(
        "CodeNode",
        [("keep::x", "keep", "x", "function", "x", "x", "f.py", "[1]", "{}", "h")],
    )
    await graph_store.bulk_copy(
        "CodeNode",
        [("add::y", "add", "y", "function", "y", "y", "g.py", "[1]", "{}", "h")],
    )

    rows = await graph_store.execute(
        "MATCH (n:CodeNode) RETURN n.project_id AS p ORDER BY p",
    )
    assert [r["p"] for r in rows] == ["add", "keep"]


@pytest.mark.integration
@pytest.mark.indexing
async def test_bulk_copy_round_trips_a_multiline_name(graph_store):
    # graphlens 0.8.x emits multi-line names for grouped imports, e.g.
    # `std::{\n  ffi::OsString,\n  path::PathBuf,\n}`. Every field is quoted, so
    # a raw newline survives verbatim rather than splitting the row.
    await persist.ensure_code_schema(graph_store)
    multiline = "std::{\n    ffi::OsString,\n    path::{Path, PathBuf},\n}"
    await graph_store.bulk_copy(
        "CodeNode",
        [
            ("p::u", "p", "u", "import", multiline, "u", "lib.rs", "[1,2]",
             "{}", "h"),
        ],
    )
    rows = await graph_store.execute(
        "MATCH (n:CodeNode {id: 'p::u'}) RETURN n.qualified_name AS qn",
    )
    assert rows[0]["qn"] == multiline


@pytest.mark.indexing
async def test_bulk_copy_rejects_a_reserved_format_char(graph_store):
    # Only the three control chars used as delimiter/quote/escape can't appear
    # in a value (they never do in real data). Newlines/commas/quotes are fine
    # now that every field is quoted — but a stray format char would corrupt
    # the frame, so fail loudly.
    await persist.ensure_code_schema(graph_store)
    with pytest.raises(GraphStoreError, match="reserved char"):
        await graph_store.bulk_copy(
            "CodeNode",
            [
                ("p::c", "p", "c", "function", "bad\x01name", "c", "f.py",
                 "[1]", "{}", "h"),
            ],
        )
