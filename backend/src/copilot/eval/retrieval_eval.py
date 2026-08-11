"""Retrieval-only evals: how good is retrieval on its own, with no LLM in the loop.

Answer quality and retrieval quality fail differently, and conflating them hides the
most common RAG failure: the right answer was impossible because the right schema
card was never retrieved. recall@k asks a narrower question than the full harness --
of the tables a question genuinely needs, how many did retrieval put in front of the
model -- and it is scored purely against `retrieve()`'s output, so it is cheap and
fast enough to run on every change to the schema cards or the retrieval query.
"""
import sys
from pathlib import Path

import yaml
from pydantic import BaseModel

from copilot.config import REPO_ROOT
from copilot.retrieval import RetrievedContext

DEFAULT_PATH = REPO_ROOT / "data" / "evals" / "retrieval.yaml"
MIN_MEAN_RECALL = 0.8


class RetrievalCase(BaseModel):
    id: str
    question: str
    must_retrieve: list[str]


def load_retrieval_cases(path: Path | None = None) -> list[RetrievalCase]:
    raw = yaml.safe_load((path or DEFAULT_PATH).read_text())
    return [RetrievalCase(**c) for c in raw["cases"]]


def score_retrieval(case: RetrievalCase, ctx: RetrievedContext) -> tuple[float, list[str]]:
    """Cards are formatted `TABLE_NAME: card text` (see retrieval.py); match each
    `must_retrieve` entry case-insensitively against the table-name prefix."""
    retrieved = {card.split(":", 1)[0].strip().upper() for card in ctx.schema_cards}
    missing = sorted(t for t in case.must_retrieve if t.upper() not in retrieved)
    found = len(case.must_retrieve) - len(missing)
    recall = found / len(case.must_retrieve) if case.must_retrieve else 1.0
    return recall, missing


def main() -> None:
    from copilot.retrieval import retrieve
    from copilot.snowflake_client import SnowflakeClient

    cases = load_retrieval_cases()
    sf = SnowflakeClient(role="COPILOT_APP_RO")
    scores: list[float] = []
    below_full: list[tuple[str, float, list[str]]] = []
    for case in cases:
        ctx = retrieve(case.question, sf)
        recall, missing = score_retrieval(case, ctx)
        scores.append(recall)
        status = "OK" if recall >= 1.0 else "MISS"
        suffix = f" missing={missing}" if missing else ""
        print(f"[{status}] {case.id} recall={recall:.2f}{suffix}")
        if recall < 1.0:
            below_full.append((case.id, recall, missing))

    mean_recall = sum(scores) / len(scores) if scores else 0.0
    print(f"\n=== Retrieval scorecard: mean recall {mean_recall:.2f} "
         f"across {len(cases)} cases ===")
    if below_full:
        print("Cases below full recall:")
        for case_id, recall, missing in below_full:
            print(f"  {case_id}: recall={recall:.2f} missing={missing}")

    if mean_recall < MIN_MEAN_RECALL:
        print(f"\nmean recall {mean_recall:.2f} is below the {MIN_MEAN_RECALL} gate")
        sys.exit(1)


if __name__ == "__main__":
    main()
