"""The judge is an LLM call, so it is faked here. What is worth testing is the
contract around it: a provider failure must fail the case, not pass it."""
from copilot.eval.judge import Verdict, judge_answer


class _Provider:
    def __init__(self, verdict=None, boom=False):
        self._v, self._boom = verdict, boom
        self.calls = []

    def structured(self, system, user, schema, max_tokens=1500):
        if self._boom:
            raise RuntimeError("anthropic is down")
        self.calls.append((system, user))
        from copilot.llm.provider import LLMResult
        return LLMResult(value=self._v, tokens_in=1, tokens_out=1)

    def text(self, system, user, max_tokens=1000):  # pragma: no cover
        raise NotImplementedError


def test_judge_returns_the_model_verdict():
    p = _Provider(Verdict(passed=True, score=0.9, reason="cites the number"))
    v = judge_answer(p, "how many machines?", "states a count", "There are 200.")
    assert v.passed and v.score == 0.9


def test_judge_prompt_carries_question_criterion_and_answer():
    p = _Provider(Verdict(passed=True, score=1.0, reason="ok"))
    judge_answer(p, "Q-MARKER", "C-MARKER", "A-MARKER")
    _system, user = p.calls[0]
    assert "Q-MARKER" in user and "C-MARKER" in user and "A-MARKER" in user


def test_judge_failure_fails_the_case_rather_than_passing_it():
    v = judge_answer(_Provider(boom=True), "q", "c", "a")
    assert v.passed is False and v.score == 0.0 and "judge unavailable" in v.reason


# --- schema: Verdict is a plain pydantic model with the fields the runner combines
# grades on, so a field rename here would silently break grade_with_judge.


def test_verdict_fields():
    v = Verdict(passed=True, score=0.75, reason="partial credit")
    assert v.passed is True
    assert v.score == 0.75
    assert v.reason == "partial credit"


def test_judge_system_prompt_instructs_strict_criterion_only_grading():
    """A judge that also grades style/verbosity is not grading the criterion --
    that drifted scope is exactly the kind of judge variance this harness exists
    to keep out."""
    p = _Provider(Verdict(passed=True, score=1.0, reason="ok"))
    judge_answer(p, "q", "c", "a")
    system, _user = p.calls[0]
    assert "criterion" in system.lower()
