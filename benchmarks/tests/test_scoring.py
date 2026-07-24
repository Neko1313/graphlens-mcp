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


def test_a_fenced_final_list_is_the_answer_not_the_reasoning():
    # An explanation that names a file in order to EXCLUDE it must not be
    # counted as listing it: the model fenced its actual answer, so that block
    # is what gets graded.
    answer = (
        "core.py only mentions it in docstrings, so it is excluded.\n"
        "```\nglobals.py\ndecorators.py\n```"
    )
    expected = {"answer_set": ["globals.py", "decorators.py"], "match": "path"}
    assert score(answer, expected) == 1.0


def test_an_unfenced_answer_is_still_graded_whole():
    answer = "globals.py\ndecorators.py"
    expected = {"answer_set": ["globals.py", "decorators.py"], "match": "path"}
    assert score(answer, expected) == 1.0


def test_a_quoted_code_snippet_is_not_mistaken_for_the_answer():
    # Models fence source they are quoting, then list the files in plain text.
    # Only a fence that ends the message delimits an answer.
    answer = (
        "Two files call it:\n\n`request.ts` — calls it here:\n"
        "```ts\nreturn parseBody(this, options)\n```\n\n"
        "request.ts\nmiddleware/method-override/index.ts"
    )
    expected = {
        "answer_set": ["request.ts", "middleware/method-override/index.ts"],
        "match": "path",
    }
    assert score(answer, expected) == 1.0


def test_the_trailing_bare_list_is_the_answer():
    # Asked for one item per line, models explain and then list. The files
    # named in the explanation to RULE THEM OUT are not claims.
    answer = (
        "The test files also import getPath, but the question excludes them.\n"
        "`src/adapter/aws-lambda/handler.ts` defines its own getPath — "
        "unrelated.\n\n"
        "src/hono-base.ts\nsrc/middleware/logger/index.ts"
    )
    expected = {
        "answer_set": ["hono-base.ts", "middleware/logger/index.ts"],
        "match": "path",
    }
    assert score(answer, expected) == 1.0


def test_a_prose_only_answer_is_still_graded_whole():
    # No fence and no bare list: nothing to narrow to, so the whole message
    # stands as the claim — a rambling answer is not rescued by the rule.
    answer = "It is called from globals.py and from decorators.py, I think."
    expected = {"answer_set": ["globals.py", "decorators.py"], "match": "path"}
    assert score(answer, expected) == 1.0
