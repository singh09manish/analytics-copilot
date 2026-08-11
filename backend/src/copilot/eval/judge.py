"""LLM judge for prose answers.

Deterministic substring checks cannot grade "does this answer actually answer the
question" -- that takes judgment. A judge can do that, at the cost of money,
latency, and its own variance, so it grades only cases that carry a `judge` string,
and its verdict is schema-checked exactly like every other structured LLM call in
this codebase (see copilot.llm.provider.AnthropicProvider.structured).

Fails closed: any provider failure -- a timeout, a schema-invalid response after
retry, an outage -- returns a failing Verdict rather than raising. A broken judge
must fail the case loudly, not silently pass it.
"""
from pydantic import BaseModel

from copilot.llm.provider import LLMProvider


class Verdict(BaseModel):
    passed: bool
    score: float
    reason: str


def _judge_system() -> str:
    return """You are grading one answer from an analytics copilot against a single
grading criterion. Judge strictly against the criterion only -- not style, not
verbosity, not phrasing you would have preferred. If the answer satisfies the
criterion, it passes, even if it could be written better.

Return:
- passed: true if the answer satisfies the criterion, false otherwise
- score: 0..1, how well the answer satisfies the criterion
- reason: one sentence explaining the verdict"""


def _judge_user(question: str, criterion: str, answer: str) -> str:
    return f"""Question asked: {question}

Grading criterion: {criterion}

Answer to grade:
{answer}

Does the answer satisfy the criterion?"""


def judge_answer(provider: LLMProvider, question: str, criterion: str,
                 answer: str) -> Verdict:
    try:
        result = provider.structured(
            system=_judge_system(),
            user=_judge_user(question, criterion, answer),
            schema=Verdict)
        return result.value
    except Exception as e:  # noqa: BLE001 — a broken judge must fail the case, not pass it
        return Verdict(passed=False, score=0.0, reason=f"judge unavailable: {e}")
