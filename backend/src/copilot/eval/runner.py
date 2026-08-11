"""Eval runner and scorecard.

`grade()` is pure and deterministic -- no I/O, no LLM -- so it is testable without
either a warehouse or a model. `run()` is the expensive part: it actually calls
`answer_question` (LLM + Snowflake) once per case, grades the result, optionally logs
it, and prints a scorecard. A `safety-` case failing is not a soft signal -- it means
a defense layer regressed -- so `run()` exits the process non-zero when that happens,
same as any other CI gate that must not be silently ignored.
"""
import logging
import subprocess
import sys
import uuid
from datetime import UTC, datetime

from copilot.agent.pipeline import ChatResponse, answer_question
from copilot.agent.prompts import PROMPT_VERSION
from copilot.config import REPO_ROOT
from copilot.eval.cases import EvalCase, load_cases

logger = logging.getLogger(__name__)

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
        passed, score, detail = grade(case, resp)
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


def main() -> None:
    from copilot.llm.provider import AnthropicProvider
    from copilot.snowflake_client import SnowflakeClient

    cases = load_cases()
    provider = AnthropicProvider()
    sf = SnowflakeClient(role="COPILOT_APP_RO")
    writer = SnowflakeClient(role="COPILOT_APP_WRITER")
    run(cases, provider, sf, writer=writer)


if __name__ == "__main__":
    main()
