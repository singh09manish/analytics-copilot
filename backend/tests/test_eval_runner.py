"""grade() is pure and deterministic, so it can be tested without an LLM or a
warehouse. That separation is the point: the expensive part is running the cases,
not judging them."""
import pytest

from copilot.agent.pipeline import ChatResponse
from copilot.eval.cases import EvalCase
from copilot.eval.judge import Verdict
from copilot.eval.runner import (
    COLUMNS,
    INSERT_SQL,
    MIN_SAFETY_CASES_IN_SUBSET,
    _pass_fraction,
    _select_subset,
    grade,
    grade_with_judge,
    run,
)
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


# --- grade_with_judge(): the deterministic grade always runs first; the judge is
# only consulted when it already passed and the case actually carries a criterion.


class _JudgeProvider:
    """Minimal LLMProvider double scripted with a Verdict, mirroring judge.py's
    own test double so a broken judge call surfaces the same way here."""

    def __init__(self, verdict):
        self._v = verdict
        self.calls = 0

    def structured(self, system, user, schema, max_tokens=1500):
        self.calls += 1
        from copilot.llm.provider import LLMResult
        return LLMResult(value=self._v, tokens_in=1, tokens_out=1)

    def text(self, system, user, max_tokens=1000):  # pragma: no cover
        raise NotImplementedError


def test_grade_with_judge_skips_the_judge_when_no_criterion():
    c = EvalCase(id="x", question="q", intent="smalltalk")
    provider = _JudgeProvider(Verdict(passed=True, score=1.0, reason="n/a"))
    passed, score, _detail = grade_with_judge(c, _resp(intent="smalltalk"), provider)
    assert passed and score == 1.0 and provider.calls == 0


def test_grade_with_judge_skips_the_judge_when_deterministic_grade_already_failed():
    c = EvalCase(id="x", question="q", intent="data_query", judge="answers the count")
    provider = _JudgeProvider(Verdict(passed=True, score=1.0, reason="n/a"))
    passed, _score, _detail = grade_with_judge(
        c, _resp(intent="smalltalk"), provider)  # intent mismatch: deterministic fail
    assert not passed and provider.calls == 0


def test_grade_with_judge_requires_both_to_pass():
    c = EvalCase(id="x", question="q", intent="data_query", judge="answers the count")
    provider = _JudgeProvider(Verdict(passed=False, score=0.2, reason="dodges the question"))
    passed, _score, detail = grade_with_judge(c, _resp(intent="data_query"), provider)
    assert not passed and "dodges the question" in detail


def test_grade_with_judge_passes_when_both_pass():
    c = EvalCase(id="x", question="q", intent="data_query", judge="answers the count")
    provider = _JudgeProvider(Verdict(passed=True, score=1.0, reason="answers clearly"))
    passed, score, _detail = grade_with_judge(c, _resp(intent="data_query"), provider)
    assert passed and score == 1.0


# --- _select_subset(): a plain alphabetical "first N by id" would starve the CI
# smoke subset of safety- cases entirely (most of this fixture's ids sort before
# "safety-"), so the guarantee is load-bearing, not decorative.


def _cases(*ids: str) -> list[EvalCase]:
    return [EvalCase(id=i, question="q", intent="data_query") for i in ids]


def test_select_subset_guarantees_minimum_safety_cases():
    cases = _cases("aaa-1", "aaa-2", "aaa-3", "aaa-4",
                   "safety-1", "safety-2", "safety-3")
    subset = _select_subset(cases, 5)
    safety_count = sum(1 for c in subset if c.id.startswith("safety-"))
    assert len(subset) == 5
    assert safety_count >= MIN_SAFETY_CASES_IN_SUBSET


def test_select_subset_is_deterministic_and_id_sorted():
    cases = _cases("zzz", "aaa", "safety-b", "safety-a", "mmm")
    subset = _select_subset(cases, 3)
    assert [c.id for c in subset] == sorted(c.id for c in subset)
    assert _select_subset(cases, 3) == subset  # same input -> same output, every time


def test_select_subset_returns_everything_when_n_covers_all_cases():
    cases = _cases("aaa", "safety-a", "zzz")
    assert _select_subset(cases, 10) == sorted(cases, key=lambda c: c.id)


def test_select_subset_still_works_with_fewer_than_two_safety_cases():
    cases = _cases("aaa-1", "aaa-2", "safety-only")
    subset = _select_subset(cases, 2)
    assert len(subset) == 2
    assert any(c.id.startswith("safety-") for c in subset)


# --- _pass_fraction(): the number --publish sends to CloudWatch as EvalAccuracy.


def test_pass_fraction_of_mixed_results():
    results = [{"passed": True}, {"passed": True}, {"passed": False}, {"passed": False}]
    assert _pass_fraction(results) == pytest.approx(0.5)


def test_pass_fraction_of_empty_results_is_zero_not_a_zero_division():
    assert _pass_fraction([]) == 0.0
