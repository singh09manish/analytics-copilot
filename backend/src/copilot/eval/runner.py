"""Eval runner and scorecard.

`grade()` is pure and deterministic -- no I/O, no LLM -- so it is testable without
either a warehouse or a model. `run()` is the expensive part: it actually calls
`answer_question` (LLM + Snowflake) once per case, grades the result, optionally logs
it, and prints a scorecard. A `safety-` case failing is not a soft signal -- it means
a defense layer regressed -- so `run()` exits the process non-zero when that happens,
same as any other CI gate that must not be silently ignored.
"""
import argparse
import logging
import subprocess
import sys
import uuid
from datetime import UTC, datetime

from copilot import metrics
from copilot.agent.pipeline import ChatResponse, answer_question
from copilot.agent.prompts import PROMPT_VERSION
from copilot.config import REPO_ROOT
from copilot.eval.cases import EvalCase, load_cases
from copilot.eval.judge import judge_answer

logger = logging.getLogger(__name__)

# --subset guarantees at least this many safety- cases regardless of where they'd
# otherwise land alphabetically. See _select_subset.
MIN_SAFETY_CASES_IN_SUBSET = 2

# Kept next to the INSERT so a hermetic test can assert
# len(COLUMNS) == sql.count("%s") == len(params), the same guard test_request_log.py
# uses for REQUEST_LOG -- a drifting count is otherwise invisible until it fails
# against a live Snowflake. Matches warehouse/bootstrap.sql's EVAL_RESULTS DDL
# exactly (created_at defaults, so it is not in this list).
COLUMNS = ("run_id", "case_id", "kind", "passed", "score", "detail",
           "git_sha", "prompt_version")
INSERT_SQL = (
    "INSERT INTO MEDTECH_ANALYTICS.COPILOT.EVAL_RESULTS "
    f"({', '.join(COLUMNS)}) VALUES ({', '.join(['%s'] * len(COLUMNS))})")


def grade(case: EvalCase, resp: ChatResponse) -> tuple[bool, float, str]:
    """Pure and deterministic: same case + same response always grades the same way.

    Order matters: an error nobody expected is disqualifying on its own (a case
    must never pass just because leftover answer text happens to match), then
    intent, then the expected error type, then substrings.
    """
    if resp.error_type and resp.error_type != case.expect_error_type:
        return False, 0.0, f"unexpected error_type={resp.error_type!r}"
    if resp.intent != case.intent:
        return False, 0.0, f"intent mismatch: expected {case.intent!r}, got {resp.intent!r}"
    if case.expect_error_type and resp.error_type != case.expect_error_type:
        return (False, 0.0,
                f"expected error_type={case.expect_error_type!r}, got {resp.error_type!r}")

    sql = (resp.sql or "").lower()
    answer = (resp.answer or "").lower()
    expectations = ([(s, sql) for s in case.expect_sql_contains]
                    + [(a, answer) for a in case.expect_answer_contains])
    if not expectations:
        return True, 1.0, "ok"
    missing = [text for text, haystack in expectations if text.lower() not in haystack]
    score = (len(expectations) - len(missing)) / len(expectations)
    if missing:
        return False, score, f"missing expectation: {missing[0]!r}"
    return True, score, "ok"


def grade_with_judge(case: EvalCase, resp: ChatResponse, provider) -> tuple[bool, float, str]:
    """`grade()` stays pure; this is the impure wrapper that spends money on a judge
    call only when the deterministic part already passed and the case carries a
    `judge` criterion. The case passes only if both the deterministic grade and the
    judge pass -- a prose answer that happens to mention the right table but never
    actually answers the question must still fail.
    """
    passed, score, detail = grade(case, resp)
    if not passed or not case.judge:
        return passed, score, detail
    verdict = judge_answer(provider, case.question, case.judge, resp.answer)
    combined_score = (score + verdict.score) / 2
    return verdict.passed, combined_score, f"judge: {verdict.reason}"


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT, text=True,
            stderr=subprocess.DEVNULL).strip()
    except Exception:  # noqa: BLE001 — git metadata is best-effort, never fatal
        return "unknown"


def _default_run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]


def _write_result(writer, run_id: str, case: EvalCase, passed: bool, score: float,
                  detail: str, git_sha: str) -> None:
    kind = "safety" if case.id.startswith("safety-") else case.intent
    try:
        writer.run_query(
            INSERT_SQL,
            (run_id, case.id, kind, passed, score, detail, git_sha, PROMPT_VERSION))
    except Exception:
        # Never let a logging failure take down the eval run itself -- the console
        # output and exit code are the source of truth either way.
        logger.warning("EVAL_RESULTS write failed for case_id=%s; the eval result "
                       "itself was unaffected.", case.id, exc_info=True)


def _print_scorecard(cases: list[EvalCase], results: list[dict]) -> None:
    total = len(results)
    passed_count = sum(1 for r in results if r["passed"])
    mean_score = sum(r["score"] for r in results) / total if total else 0.0
    print(f"\n=== Eval scorecard: {passed_count}/{total} passed, "
         f"mean score {mean_score:.2f} ===")
    by_intent: dict[str, list[dict]] = {}
    for case, result in zip(cases, results, strict=True):
        by_intent.setdefault(case.intent, []).append(result)
    for intent, rows in sorted(by_intent.items()):
        p = sum(1 for r in rows if r["passed"])
        m = sum(r["score"] for r in rows) / len(rows)
        print(f"  {intent}: {p}/{len(rows)} passed, mean score {m:.2f}")


def run(cases: list[EvalCase], provider, sf, *, writer=None,
       run_id: str | None = None) -> list[dict]:
    run_id = run_id or _default_run_id()
    git_sha = _git_sha()
    results: list[dict] = []
    for case in cases:
        resp = answer_question(case.question, provider, sf)
        passed, score, detail = grade_with_judge(case, resp, provider)
        result = {"run_id": run_id, "case_id": case.id, "passed": passed,
                  "score": score, "detail": detail}
        results.append(result)
        print(f"[{'PASS' if passed else 'FAIL'}] {case.id} "
             f"score={score:.2f} {detail}")
        if writer is not None:
            _write_result(writer, run_id, case, passed, score, detail, git_sha)
    _print_scorecard(cases, results)

    safety_failed = [r for r in results if not r["passed"]
                     and r["case_id"].startswith("safety-")]
    if safety_failed:
        ids = ", ".join(r["case_id"] for r in safety_failed)
        print(f"\nSAFETY REGRESSION: {len(safety_failed)} guard case(s) failed: {ids}")
        sys.exit(1)
    return results


def _select_subset(cases: list[EvalCase], n: int) -> list[EvalCase]:
    """Deterministic CI-cheap subset: sorted by id (not YAML order), so the same N
    cases run every time -- with at least MIN_SAFETY_CASES_IN_SUBSET `safety-`
    cases guaranteed in the mix.

    A plain "first N sorted by id" cannot deliver that guarantee by itself: of
    this golden set's ids, 18 sort before the `safety-` prefix, so a naive
    `sorted(cases, key=...)[:5]` would smoke-test zero guard cases -- exactly the
    false sense of coverage a smoke subset must not have, since the safety- cases
    are the ones that catch a guard regression and are the reason this subset
    exists. Filling the safety quota first and the rest with whatever's next in
    id order keeps the selection fully deterministic without renumbering the
    other 30-odd cases just to win a sort.
    """
    ordered = sorted(cases, key=lambda c: c.id)
    if n >= len(ordered):
        return ordered
    safety = [c for c in ordered if c.id.startswith("safety-")][:MIN_SAFETY_CASES_IN_SUBSET]
    other_slots = max(n - len(safety), 0)
    other = [c for c in ordered if not c.id.startswith("safety-")][:other_slots]
    return sorted(safety + other, key=lambda c: c.id)


def _pass_fraction(results: list[dict]) -> float:
    return sum(1 for r in results if r["passed"]) / len(results) if results else 0.0


def _publish_metrics(accuracy: float, retrieval_recall: float) -> None:
    """Publish the weekly drift numbers to the same namespace the live app emits
    EMF metrics into, so accuracy is a line on the dashboard next to real traffic
    instead of a number buried in a CI log.

    The asymmetry with metrics.py is deliberate, not an inconsistency: the app
    emits EMF because it already ships stdout to CloudWatch Logs via the ECS
    task's awslogs driver and needs no boto3 and no IAM permission for it (see
    metrics.py). This job has no log stream to piggyback on -- it runs in GitHub
    Actions, not in the ECS task -- so it calls PutMetricData directly, which is
    exactly why infra/iam.tf grants the GitHub deploy role a narrow,
    namespace-scoped cloudwatch:PutMetricData permission that the app's own task
    role does not need.

    One API call carrying both data points, not two calls, so a partial publish
    can't leave the dashboard with an accuracy line and no matching recall line
    for the same run.
    """
    import boto3

    client = boto3.client("cloudwatch")
    client.put_metric_data(
        Namespace=metrics.NAMESPACE,
        MetricData=[
            {"MetricName": "EvalAccuracy", "Value": accuracy, "Unit": "None"},
            {"MetricName": "EvalRetrievalRecall", "Value": retrieval_recall, "Unit": "None"},
        ],
    )


def main() -> None:
    from copilot.llm.provider import AnthropicProvider
    from copilot.snowflake_client import SnowflakeClient

    parser = argparse.ArgumentParser(
        description="Run the golden eval set against the live pipeline and print a scorecard.")
    parser.add_argument(
        "--subset", type=int, default=None, metavar="N",
        help="Run a deterministic N-case subset (sorted by id, at least "
             f"{MIN_SAFETY_CASES_IN_SUBSET} safety- cases guaranteed) instead of "
             "the full golden set -- for a cheap CI smoke check.")
    parser.add_argument(
        "--publish", action="store_true",
        help="After the run, also score the retrieval eval set and publish "
             "EvalAccuracy and EvalRetrievalRecall to CloudWatch "
             f"(namespace {metrics.NAMESPACE}).")
    args = parser.parse_args()

    cases = load_cases()
    if args.subset is not None:
        cases = _select_subset(cases, args.subset)

    provider = AnthropicProvider()
    sf = SnowflakeClient(role="COPILOT_APP_RO")
    writer = SnowflakeClient(role="COPILOT_APP_WRITER")
    # run() calls sys.exit(1) here if a safety case failed, before --publish ever
    # runs. That's intentional: a failed CI job is a louder signal than a missing
    # data point on the drift dashboard for that week.
    results = run(cases, provider, sf, writer=writer)

    if args.publish:
        from copilot.eval.retrieval_eval import load_retrieval_cases, score_retrieval
        from copilot.retrieval import retrieve

        accuracy = _pass_fraction(results)
        retrieval_cases = load_retrieval_cases()
        recalls = [score_retrieval(c, retrieve(c.question, sf))[0] for c in retrieval_cases]
        retrieval_recall = sum(recalls) / len(recalls) if recalls else 0.0
        _publish_metrics(accuracy, retrieval_recall)


if __name__ == "__main__":
    main()
