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
    intent: str
    expect_sql_contains: list[str] = []
    expect_answer_contains: list[str] = []
    expect_error_type: str | None = None
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
