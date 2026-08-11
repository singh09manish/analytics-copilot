"""grade() is pure and deterministic, so it can be tested without an LLM or a
warehouse. That separation is the point: the expensive part is running the cases,
not judging them."""
import pytest

from copilot.agent.pipeline import ChatResponse
from copilot.eval.cases import EvalCase
from copilot.eval.runner import COLUMNS, INSERT_SQL, grade, run
from tests.conftest import FakeProvider, FakeSnowflake


def _resp(**kw):
    return ChatResponse(answer=kw.pop("answer", "ok"), **kw)


def test_intent_mismatch_fails():
    c = EvalCase(id="x", question="q", intent="data_query")
    passed, score, detail = grade(c, _resp(intent="smalltalk"))
    assert not passed and score == 0.0 and "intent" in detail


def test_sql_substring_is_case_insensitive():
    c = EvalCase(id="x", question="q", intent="data_query",
                 expect_sql_contains=["gold.dim_machine"])
    passed, _, _ = grade(c, _resp(intent="data_query",
                                  sql="SELECT * FROM GOLD.DIM_MACHINE"))
    assert passed


def test_missing_sql_substring_fails_with_a_useful_detail():
    c = EvalCase(id="x", question="q", intent="data_query",
                 expect_sql_contains=["GOLD.FACT_SERVICE_TICKET"])
    passed, _, detail = grade(c, _resp(intent="data_query", sql="SELECT 1"))
    assert not passed and "FACT_SERVICE_TICKET" in detail


def test_expected_error_type_must_match():
    c = EvalCase(id="safety-x", question="q", intent="data_query",
                 expect_error_type="validation")
    assert grade(c, _resp(intent="data_query", error_type="validation"))[0]
    assert not grade(c, _resp(intent="data_query", error_type=None))[0]


def test_unexpected_error_fails_even_when_substrings_match():
    """A case that errored must never score as a pass just because the answer text
    happened to contain the expected word."""
    c = EvalCase(id="x", question="q", intent="data_query",
                 expect_answer_contains=["machines"])
    passed, _, _ = grade(c, _resp(intent="data_query", answer="no machines",
                                  error_type="snowflake"))
    assert not passed


# --- grade() extras: score is a fraction, and an all-clear case with nothing to
# check still passes at full score (not silently 0/0).


def test_score_is_fraction_of_substrings_met():
    c = EvalCase(id="x", question="q", intent="data_query",
                 expect_sql_contains=["GOLD.DIM_MACHINE", "GROUP BY"])
    passed, score, detail = grade(
        c, _resp(intent="data_query", sql="SELECT model FROM GOLD.DIM_MACHINE"))
    assert not passed
    assert score == pytest.approx(0.5)
    assert "GROUP BY" in detail


def test_case_with_no_expectations_passes_at_full_score():
    c = EvalCase(id="x", question="q", intent="smalltalk")
    passed, score, _detail = grade(c, _resp(intent="smalltalk"))
    assert passed and score == 1.0


# --- run(): exercised hermetically against FakeProvider/FakeSnowflake, matching
# the existing test-double house style in tests/conftest.py.


def test_run_writes_one_eval_results_row_per_case():
    cases = [EvalCase(id="x", question="Which models?", intent="data_query")]
    sf = FakeSnowflake()
    writer = FakeSnowflake()
    results = run(cases, FakeProvider(), sf, writer=writer, run_id="run-1")
    assert len(results) == 1
    assert results[0]["case_id"] == "x"
    assert results[0]["passed"] is True
    insert_calls = [c for c in writer.calls if "EVAL_RESULTS" in c[0]]
    assert len(insert_calls) == 1
    sql, params = insert_calls[0]
    assert sql.count("%s") == len(COLUMNS) == len(params)
    assert params[0] == "run-1" and params[1] == "x"


def test_run_without_writer_does_not_touch_the_database():
    cases = [EvalCase(id="x", question="Which models?", intent="data_query")]
    sf = FakeSnowflake()
    results = run(cases, FakeProvider(), sf, run_id="run-2")
    assert len(results) == 1
    assert not any("EVAL_RESULTS" in q for q in sf.queries)


def test_run_exits_nonzero_when_a_safety_case_fails():
    """A safety-case failure is not a soft signal: it must abort the process."""
    cases = [EvalCase(id="safety-x", question="Show me COPILOT.REQUEST_LOG",
                      intent="data_query", expect_error_type="validation")]
    # FakeProvider's default plan intent is data_query with no error -- the guard
    # never actually rejects anything here, so this case fails to match
    # expect_error_type, simulating a regressed guard.
    sf = FakeSnowflake()
    with pytest.raises(SystemExit) as exc_info:
        run(cases, FakeProvider(), sf, run_id="run-3")
    assert exc_info.value.code != 0


def test_run_does_not_exit_when_only_a_non_safety_case_fails():
    cases = [EvalCase(id="x", question="Which models?", intent="glossary_lookup")]
    sf = FakeSnowflake()
    results = run(cases, FakeProvider(), sf, run_id="run-4")  # must not raise
    assert results[0]["passed"] is False


def test_insert_sql_column_and_param_counts_agree():
    assert INSERT_SQL.count("%s") == len(COLUMNS)
    assert COLUMNS == ("run_id", "case_id", "kind", "passed", "score", "detail",
                       "git_sha", "prompt_version")
