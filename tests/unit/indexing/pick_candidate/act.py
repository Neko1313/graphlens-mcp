import pytest

from shared.common.indexing.query import _pick


def _node(local_id: str, kind: str) -> dict[str, object]:
    return {"id": local_id, "name": "parseBody", "kind": kind}


@pytest.mark.unit
@pytest.mark.indexing
def test_a_lone_definition_wins_past_its_phantom_and_imports():
    # The real bug: a re-exported symbol yields one function plus a phantom
    # (external_symbol) and several imports of the same name. The function must
    # resolve outright, not come back as an ambiguous six-way list.
    node_id, candidates = _pick(
        [
            _node("phantom", "external_symbol"),
            _node("imp1", "import"),
            _node("imp2", "import"),
            _node("real", "function"),
        ],
    )

    assert node_id == "real"
    assert candidates == []


@pytest.mark.unit
@pytest.mark.indexing
def test_two_real_definitions_stay_ambiguous_but_shed_the_noise():
    # A method and a free function share the name: genuinely ambiguous, so the
    # caller gets a list — but only the two real symbols, never the phantom.
    node_id, candidates = _pick(
        [
            _node("phantom", "external_symbol"),
            _node("imp", "import"),
            _node("method", "method"),
            _node("func", "function"),
        ],
    )

    assert node_id is None
    assert {c["kind"] for c in candidates} == {"method", "function"}


@pytest.mark.unit
@pytest.mark.indexing
def test_a_purely_external_symbol_still_resolves_as_a_last_resort():
    # If nothing but noise carries the name, dropping it would leave the caller
    # with nothing — so a lone external symbol is kept and resolved.
    node_id, candidates = _pick([_node("only", "external_symbol")])

    assert node_id == "only"
    assert candidates == []
