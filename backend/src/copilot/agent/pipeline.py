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
    # "vector" (Cortex) or "keyword" (fallback). Surfaced so the README's "Cortex
    # vector retrieval" claim is falsifiable from a response, not only from a log
    # line. Additive and optional -- existing clients ignore it.
    retrieval_mode: str | None = None


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
             "tokens_in": 0, "tokens_out": 0, "retrieval_ms": 0,
             "retrieval_mode": ""},
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
    # The response construction is inside a try because pydantic's ValidationError is
    # a ValueError, which would escape this function and break the never-raises
    # contract if any state value ever failed ChatResponse's schema. It cannot fire
    # today; wrapping it makes the contract structural rather than incidental.
    try:
        # exec_error is set by execute()'s first failure and never cleared once a
        # repair cycle routes past it (generate()/do_validate() go straight to
        # remember on their own failure, skipping execute). Guard with "not
        # error_type" so a node's explicit llm/validation classification always wins
        # over that stale exec_error instead of being overwritten by the generic
        # warehouse-retry message below.
        if state.get("exec_error") and not state.get("error_type"):
            return ChatResponse(
                answer="The query failed against the warehouse even after a retry. "
                       "Try asking a bit differently.",
                sql=state.get("safe_sql") or state.get("draft_sql"),
                error_type="snowflake", intent=state.get("intent") or None,
                retrieval_ms=state.get("retrieval_ms", 0), request_id=request_id,
                retrieval_mode=state.get("retrieval_mode") or None,
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
            retrieval_mode=state.get("retrieval_mode") or None,
            tokens_in=state.get("tokens_in", 0), tokens_out=state.get("tokens_out", 0),
            request_id=request_id)
    except Exception:  # noqa: BLE001 — answer_question NEVER raises
        return ChatResponse(
            answer="I couldn't assemble a response for that. Please try again.",
            error_type="llm", request_id=request_id)
