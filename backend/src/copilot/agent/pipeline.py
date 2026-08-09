from pydantic import BaseModel

from copilot.llm.provider import LLMProvider
from copilot.snowflake_client import SnowflakeClient
from copilot.sql_guard import SqlGuardError


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

    request_id = uuid.uuid4().hex
    use_memory = conversation_id is not None
    try:
        # Deferred so a langgraph import/build failure is caught below instead of
        # escaping answer_question as a raw exception (never-raises contract).
        from copilot.agent.graph import build_graph

        graph = build_graph(provider, sf, executor=executor, use_memory=use_memory)
        invoke_kwargs = {}
        if use_memory:
            invoke_kwargs["config"] = {"configurable": {"thread_id": conversation_id}}
        state = graph.invoke(
            {"question": question, "intent": "", "context": {}, "draft_sql": "",
             "assumptions": [], "safe_sql": "", "columns": [], "rows": [],
             "exec_error": "", "repair_count": 0, "answer": "", "error_type": "",
             "tokens_in": 0, "tokens_out": 0, "retrieval_ms": 0},
            **invoke_kwargs)
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
            error_type="snowflake", intent=state.get("intent") or None,
            retrieval_ms=state.get("retrieval_ms", 0), request_id=request_id,
            tokens_in=state.get("tokens_in", 0), tokens_out=state.get("tokens_out", 0))
    is_data = state.get("intent") == "data_query"
    return ChatResponse(
        answer=state.get("answer", ""),
        sql=(state.get("safe_sql") or state.get("draft_sql") or None) if is_data else None,
        columns=state.get("columns", []) if is_data else [],
        rows=state.get("rows", [])[:200] if is_data else [],
        assumptions=state.get("assumptions", []) if is_data else [],
        error_type=state.get("error_type") or None,
        intent=state.get("intent") or None, retrieval_ms=state.get("retrieval_ms", 0),
        tokens_in=state.get("tokens_in", 0), tokens_out=state.get("tokens_out", 0),
        request_id=request_id)
