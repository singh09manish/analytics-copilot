"""Fire-and-forget REQUEST_LOG writes. Telemetry must never break a request."""
from copilot.agent.pipeline import ChatResponse
from copilot.agent.prompts import PROMPT_VERSION


def log_request(sf_writer, *, request_id: str, conversation_id: str | None,
                user_role: str, question: str, response: ChatResponse,
                e2e_ms: int) -> None:
    try:
        sf_writer.run_query(
            "INSERT INTO MEDTECH_ANALYTICS.COPILOT.REQUEST_LOG "
            "(request_id, conversation_id, user_role, question, intent, sql_text, "
            "status, error_type, e2e_ms, retrieval_ms, tokens_in, tokens_out, "
            "prompt_version) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (request_id, conversation_id, user_role, question[:1000],
             response.intent, response.sql, "error" if response.error_type else "ok",
             response.error_type, e2e_ms, response.retrieval_ms,
             response.tokens_in, response.tokens_out, PROMPT_VERSION))
    except Exception:  # noqa: BLE001, S110 — logging is best-effort by design, never raises
        pass
