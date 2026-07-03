"""Unit tests for the deterministic grader."""

from __future__ import annotations

import pytest

from bench.scoring import is_error, score


def test_answer_contains_all_present():
    assert score("gin.go", {"answer_contains": ["gin.go"]}) == 1.0


def test_answer_contains_case_insensitive_partial():
    s = score(
        "Found in Gin.GO only", {"answer_contains": ["gin.go", "engine.go"]}
    )
    assert s == 0.5


def test_answer_contains_path_substring():
    ans = "The class is in superset/models/dashboard.py at line 131."
    assert (
        score(ans, {"answer_contains": ["superset/models/dashboard.py"]})
        == 1.0
    )


def test_error_answer_scores_zero():
    assert score("__NO_TOOLS__ blah", {"answer_contains": ["x"]}) == 0.0
    assert is_error("__RUN_ERROR__ boom")
    assert not is_error("RouterGroup")


def test_camel_set_perfect():
    gold = ["BigQueryEngineSpec", "PrestoEngineSpec", "TrinoEngineSpec"]
    ans = "BigQueryEngineSpec\nPrestoEngineSpec\nTrinoEngineSpec"
    assert score(ans, {"answer_set": gold}) == pytest.approx(1.0)


def test_camel_set_over_listing_penalized():
    gold = ["BigQueryEngineSpec", "PrestoEngineSpec"]
    # Lists two correct + three bogus CamelCase => precision 2/5, recall 2/2.
    ans = "BigQueryEngineSpec PrestoEngineSpec MySQLEngineSpec SqliteEngineSpec OracleEngineSpec"
    s = score(ans, {"answer_set": gold})
    assert 0.5 < s < 0.7  # F1 = 2*0.4*1/(1.4) ~= 0.571


def test_path_set_recall_miss():
    gold = ["a/b.py", "c/d.py", "e/f.py"]
    ans = "a/b.py\nc/d.py"  # missing e/f.py => recall 2/3, precision 1
    s = score(ans, {"answer_set": gold})
    assert s == pytest.approx(2 * 1.0 * (2 / 3) / (1.0 + 2 / 3))


def test_path_set_normalizes_leading_dotslash_and_backticks():
    gold = ["src/router.ts"]
    ans = "`./src/router.ts`"
    assert score(ans, {"answer_set": gold}) == pytest.approx(1.0)


def test_line_mode_snake_case():
    gold = ["get_example_database", "load_examples"]
    ans = "- get_example_database\n- load_examples"
    assert score(ans, {"answer_set": gold, "match": "line"}) == pytest.approx(
        1.0
    )
