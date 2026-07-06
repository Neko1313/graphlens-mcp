"""
Unit tests for the lean tool layer's self-correcting signals.

Covers three fixes:
- the repeat-guard's TTL (a call seen long ago, in an unrelated session,
  must not pre-block a genuinely first attempt in a new one)
- `search`'s `note` firing on any 100%-semantic-guess result, not just when
  the query happens to contain specific punctuation
- `relations`'s `note` flagging "callers empty, references not" — usage
  through something other than a direct call (JSX, a decorator, DI, ...)
"""

from __future__ import annotations

import time

import model2vec
import numpy as np
import pytest
from graphlens import RelationKind

from graphlens_mcp.indexer.workspace import _REPEAT_TTL_SECONDS, Workspace
from graphlens_mcp.server.lean import tool_relations, tool_search
from tests.conftest import graph_of, make_node, make_relation

pytestmark = [pytest.mark.unit, pytest.mark.tools]


# ---- repeat-guard TTL -----------------------------------------------------
#
# The real event loop (and pytest-asyncio) relies on time.monotonic() for its
# own scheduling, so these tests avoid monkeypatching it globally — instead
# they let note_call run against the real clock, then rewrite the recorded
# timestamp directly to simulate an old sighting.


async def test_note_call_ttl_expires_old_repeats(store, tmp_path):
    ws = Workspace(store, tmp_path)

    assert ws.note_call("search", "k") == 0
    assert ws.note_call("search", "k") == 1
    assert ws.note_call("search", "k") == 2

    # Back-date the last sighting past the TTL, simulating a repeat left
    # over from an unrelated, long-past session on this same long-lived
    # server process.
    count, _ = ws._seen_calls[("search", "k")]
    ws._seen_calls[("search", "k")] = (
        count,
        time.monotonic() - _REPEAT_TTL_SECONDS - 1,
    )

    assert ws.note_call("search", "k") == 0


async def test_note_call_within_ttl_keeps_counting(store, tmp_path):
    ws = Workspace(store, tmp_path)

    assert ws.note_call("search", "k") == 0
    assert ws.note_call("search", "k") == 1
    assert ws.note_call("search", "k") == 2


# ---- relations() note: callers empty, references not ---------------------


async def test_relations_note_flags_reference_only_usage(store, tmp_path):
    target = make_node("pkg.get_snapshot", file_path="/proj/routes.py")
    boundary_user = make_node("pkg.router_setup", file_path="/proj/routes.py")
    await store.apply_patch(
        graph_of(
            [target, boundary_user],
            [make_relation(boundary_user, target, RelationKind.REFERENCES)],
        ),
        "/proj/routes.py",
        "hash",
        1.0,
        10,
        "ok",
        "python",
    )
    ws = Workspace(store, tmp_path)

    result = await tool_relations(store, ws, target.id)

    assert result.callers == []
    assert result.references_total == 1
    assert result.note is not None
    assert "references" in result.note


async def test_relations_note_absent_when_callers_present(store, tmp_path):
    target = make_node("pkg.helper", file_path="/proj/a.py")
    caller = make_node("pkg.main", file_path="/proj/a.py")
    await store.apply_patch(
        graph_of(
            [target, caller],
            [make_relation(caller, target, RelationKind.CALLS)],
        ),
        "/proj/a.py",
        "hash",
        1.0,
        10,
        "ok",
        "python",
    )
    ws = Workspace(store, tmp_path)

    result = await tool_relations(store, ws, target.id)

    assert result.callers != []
    assert result.note is None


async def test_relations_note_absent_when_nothing_references_it(
    store, tmp_path
):
    target = make_node("pkg.unused", file_path="/proj/a.py")
    await store.apply_patch(
        graph_of([target], []),
        "/proj/a.py",
        "hash",
        1.0,
        10,
        "ok",
        "python",
    )
    ws = Workspace(store, tmp_path)

    result = await tool_relations(store, ws, target.id)

    assert result.callers == []
    assert result.references_total == 0
    assert result.note is None


# ---- search() note: fires on a 100%-semantic-guess result -----------------


class _FakeModel:
    @staticmethod
    def from_pretrained(_id):
        return _FakeModel()

    def encode(self, texts):
        return np.ones((len(texts), 3), dtype=np.float32)


@pytest.mark.parametrize(
    "query", ["totallyunrelatedword", "message=", "foo_bar"]
)
async def test_search_note_fires_without_literal_looking_punctuation(
    store, tmp_path, monkeypatch, query
):
    monkeypatch.setattr(model2vec, "StaticModel", _FakeModel)
    await store._conn.execute(
        "INSERT INTO files(path, hash, mtime, size, status, language) "
        "VALUES('/x/a.py', 'abc', 1.0, 10, 'ok', 'python')"
    )
    await store._conn.execute(
        "INSERT INTO nodes(id, kind, qualified_name, name, file_path) "
        "VALUES('n1', 'function', 'pkg.helper', 'helper', '/x/a.py')"
    )
    await store._conn.commit()
    ws = Workspace(store, tmp_path)
    avail = await ws.semantic.build(store)
    assert avail.ok is True

    result = await tool_search(store, ws, query)

    assert result.via and all(v == "meaning" for v in result.via)
    assert result.note is not None
