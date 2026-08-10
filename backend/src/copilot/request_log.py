"""REQUEST_LOG writes. Best-effort: telemetry must never break a request.

Called synchronously on /chat's critical path (see api/main.py) -- it is best-effort,
not backgrounded, so a slow writer session does add to end-to-end latency.
"""
import logging

from copilot.agent.pipeline import ChatResponse
from copilot.agent.prompts import PROMPT_VERSION

logger = logging.getLogger(__name__)

# Kept next to the INSERT so the hermetic test can assert
# len(COLUMNS) == sql.count("%s") == len(params). A drifting count is otherwise
# invisible until it fails against a live Snowflake, where the failure is swallowed.
COLUMNS = ("request_id", "conversation_id", "user_role", "question", "intent",
           "sql_text", "status", "error_type", "e2e_ms", "retrieval_ms",
           "tokens_in", "tokens_out", "prompt_version")
INSERT_SQL = (
    "INSERT INTO MEDTECH_ANALYTICS.COPILOT.REQUEST_LOG "
    f"({', '.join(COLUMNS)}) VALUES ({', '.join(['%s'] * len(COLUMNS))})")


def log_request(sf_writer, *, request_id: str, conversation_id: str | None,
                user_role: str, question: str, response: ChatResponse,
                e2e_ms: int) -> None:
    try:
        sf_writer.run_query(
            INSERT_SQL,
            (request_id, conversation_id, user_role, question[:1000],
             response.intent, response.sql, "error" if response.error_type else "ok",
             response.error_type, e2e_ms, response.retrieval_ms,
             response.tokens_in, response.tokens_out, PROMPT_VERSION))
    except Exception:
        # Never raises -- but never silent either. A wrong param count, a missing
        # INSERT grant or a VARCHAR overflow used to produce zero signal anywhere
        # while the README claimed every request was logged.
        logger.warning("REQUEST_LOG write failed for request_id=%s; the request itself "
                       "was unaffected.", request_id, exc_info=True)
