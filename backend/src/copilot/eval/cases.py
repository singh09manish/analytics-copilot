"""The golden set: questions with known-good expectations.

Deliberately mixed. Deterministic checks (does the SQL mention this table, does the
answer contain this number) grade most cases without an LLM. A `judge` string is only
used where the right answer is prose, because an LLM judge is slower, costs money,
and is itself a source of variance.
"""
from pathlib import Path

import yaml
from pydantic import BaseModel

from copilot.config import REPO_ROOT

DEFAULT_PATH = REPO_ROOT / "data" / "evals" / "golden.yaml"


class EvalCase(BaseModel):
    id: str
    question: str
    # None for an `expect_refused` case: what actually refuses the request (planner,
    # guard, or execution) is an implementation detail the case must not pin -- see
    # `expect_refused` below and docs/DECISIONS.md sec. 12, "assert the outcome, not
    # the layer that produces it."
    intent: str | None = None
    expect_sql_contains: list[str] = []
    expect_answer_contains: list[str] = []
    expect_error_type: str | None = None
    # True for a case whose only requirement is that the request was refused --
    # by ANY layer (planner classifies it unsupported, the sqlglot guard rejects
    # the generated SQL, or execution fails) -- and that no rows of data came back.
    # Grading skips the `intent` check entirely for these cases (see grade() in
    # runner.py); which layer did the refusing is not the thing being tested.
    expect_refused: bool = False
    judge: str | None = None


def load_cases(path: Path | None = None) -> list[EvalCase]:
    raw = yaml.safe_load((path or DEFAULT_PATH).read_text())
    cases = [EvalCase(**c) for c in raw["cases"]]
    seen: set[str] = set()
    for c in cases:
        if c.id in seen:
            raise ValueError(f"duplicate case id: {c.id}")
        seen.add(c.id)
    return cases
