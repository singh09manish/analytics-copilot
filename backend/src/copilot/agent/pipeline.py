from pydantic import BaseModel

from copilot.llm.provider import LLMProvider
from copilot.snowflake_client import SnowflakeClient


class ChatResponse(BaseModel):
    answer: str
    sql: str | None = None
    columns: list[str] = []
    rows: list[list] = []
    assumptions: list[str] = []
    error_type: str | None = None
    retrieval_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    intent: str | None = None
    request_id: str | None = None


def answer_question(question: str, provider: LLMProvider, sf: SnowflakeClient, *,
                    conversation_id: str | None = None, executor=None) -> ChatResponse:
    import uuid

    from copilot.agent.graph import build_graph
    from copilot.sql_guard import SqlGuardError

    request_id = uuid.uuid4().hex
    thread = conversation_id or request_id
    graph = build_graph(provider, sf, executor=executor)
    try:
        state = graph.invoke(
            {"question": question, "intent": "", "context": {}, "draft_sql": "",
             "assumptions": [], "safe_sql": "", "columns": [], "rows": [],
             "exec_error": "", "repair_count": 0, "answer": "", "error_type": "",
             "tokens_in": 0, "tokens_out": 0, "retrieval_ms": 0},
            config={"configurable": {"thread_id": thread}})
    except SqlGuardError as e:
        return ChatResponse(
            answer=f"I generated a query the safety rules rejected ({e.reason}). "
                   "Try rephrasing your question.",
            error_type="validation", request_id=request_id)
    except ValueError:
        return ChatResponse(
            answer="I couldn't turn that into a query. Try rephrasing with the "
                   "metric and time range you care about.",
            error_type="llm", request_id=request_id)
    except Exception:  # noqa: BLE001 — total outage must degrade, not crash
        return ChatResponse(
            answer="I couldn't reach the warehouse to look up context. Please try "
                   "again in a moment.",
            error_type="snowflake", request_id=request_id)
    if state.get("exec_error"):
        return ChatResponse(
            answer="The query failed against the warehouse even after a retry. "
                   "Try asking a bit differently.",
            sql=state.get("safe_sql") or state.get("draft_sql"),
            error_type="snowflake", intent=state.get("intent"),
            retrieval_ms=state.get("retrieval_ms", 0), request_id=request_id,
            tokens_in=state.get("tokens_in", 0), tokens_out=state.get("tokens_out", 0))
    is_data = state.get("intent") == "data_query"
    return ChatResponse(
        answer=state.get("answer", ""),
        sql=state.get("safe_sql") or None if is_data else None,
        columns=state.get("columns", []) if is_data else [],
        rows=state.get("rows", [])[:200] if is_data else [],
        assumptions=state.get("assumptions", []) if is_data else [],
        error_type=state.get("error_type") or None,
        intent=state.get("intent"), retrieval_ms=state.get("retrieval_ms", 0),
        tokens_in=state.get("tokens_in", 0), tokens_out=state.get("tokens_out", 0),
        request_id=request_id)
