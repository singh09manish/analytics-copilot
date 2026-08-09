"""LangGraph agent: plan -> (route) -> retrieve -> generate -> validate -> execute
-> summarize, with one repair cycle and per-conversation memory."""
import csv
import io
from typing import TypedDict

from langgraph.graph import END, StateGraph

from copilot.agent import prompts
from copilot.llm.schemas import QueryPlan, SqlDraft
from copilot.retrieval import RetrievedContext, retrieve
from copilot.sql_guard import validate

MAX_SUMMARY_ROWS = 50
SCOPE_MESSAGE = ("I answer questions about the analytics warehouse - machines, "
                 "treatment centers, utilization, and service tickets. Try asking "
                 "about downtime, delivered fractions, or open tickets.")


class AgentState(TypedDict, total=False):
    question: str
    history: list  # [(q, a), ...] persisted by checkpointer
    intent: str
    context: dict  # RetrievedContext.model_dump()
    draft_sql: str
    assumptions: list
    safe_sql: str
    columns: list
    rows: list
    exec_error: str
    repair_count: int
    answer: str
    error_type: str
    tokens_in: int
    tokens_out: int
    retrieval_ms: int


def _rows_as_csv(columns: list, rows: list) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(columns)
    w.writerows(rows[:MAX_SUMMARY_ROWS])
    return buf.getvalue()


def build_graph(provider, sf, executor=None):
    run_sql = executor or sf.run_query

    def plan(state: AgentState) -> dict:
        user = prompts.user_with_history(state["question"], state.get("history", []))
        res = provider.structured(system=prompts.plan_system(), user=user,
                                  schema=QueryPlan, max_tokens=300)
        return {"intent": res.value.intent,
                "tokens_in": state.get("tokens_in", 0) + res.tokens_in,
                "tokens_out": state.get("tokens_out", 0) + res.tokens_out}

    def scope_reply(state: AgentState) -> dict:
        if state["intent"] == "smalltalk":
            return {"answer": "Hi! " + SCOPE_MESSAGE}
        return {"answer": SCOPE_MESSAGE}

    def do_retrieve(state: AgentState) -> dict:
        ctx = retrieve(state["question"], sf)
        return {"context": ctx.model_dump(), "retrieval_ms": ctx.retrieval_ms}

    def glossary_answer(state: AgentState) -> dict:
        ctx = RetrievedContext.model_validate(state["context"])
        glossary = "\n".join(f"- {g}" for g in ctx.glossary)
        res = provider.text(system=prompts.glossary_system(),
                            user=f"Glossary entries:\n{glossary}\n\n"
                                 f"Question: {state['question']}")
        return {"answer": res.value,
                "tokens_in": state.get("tokens_in", 0) + res.tokens_in,
                "tokens_out": state.get("tokens_out", 0) + res.tokens_out}

    def generate(state: AgentState) -> dict:
        ctx = RetrievedContext.model_validate(state["context"])
        user = prompts.user_with_history(state["question"], state.get("history", []))
        if state.get("exec_error"):
            user += (f"\n\nYour previous SQL failed with this Snowflake error - "
                     f"fix it:\n{state['exec_error']}\nPrevious SQL:\n{state['draft_sql']}")
        res = provider.structured(system=prompts.sql_system(ctx), user=user,
                                  schema=SqlDraft)
        draft: SqlDraft = res.value
        return {"draft_sql": draft.sql, "assumptions": draft.assumptions,
                "tokens_in": state.get("tokens_in", 0) + res.tokens_in,
                "tokens_out": state.get("tokens_out", 0) + res.tokens_out}

    def do_validate(state: AgentState) -> dict:
        return {"safe_sql": validate(state["draft_sql"])}

    def execute(state: AgentState) -> dict:
        try:
            columns, rows = run_sql(state["safe_sql"])
            return {"columns": list(columns), "rows": [list(r) for r in rows],
                    "exec_error": ""}
        except Exception as e:  # noqa: BLE001 — routed to repair or graceful error
            return {"exec_error": str(e)[:500],
                    "repair_count": state.get("repair_count", 0) + 1}

    def summarize(state: AgentState) -> dict:
        try:
            res = provider.text(
                system=prompts.summarize_system(),
                user=f"Question: {state['question']}\n\nSQL:\n{state['safe_sql']}\n\n"
                     f"Results (CSV, first {MAX_SUMMARY_ROWS} rows):\n"
                     f"{_rows_as_csv(state['columns'], state['rows'])}")
            return {"answer": res.value,
                    "tokens_in": state.get("tokens_in", 0) + res.tokens_in,
                    "tokens_out": state.get("tokens_out", 0) + res.tokens_out}
        except Exception:  # noqa: BLE001 — keep the data even if summarization fails
            return {"answer": "I ran the query successfully but couldn't generate a "
                              "summary. The results are shown below.",
                    "error_type": "llm"}

    def remember(state: AgentState) -> dict:
        history = list(state.get("history", []))
        history.append((state["question"], state.get("answer", "")[:500]))
        return {"history": history[-6:]}

    g = StateGraph(AgentState)
    g.add_node("plan", plan)
    g.add_node("scope_reply", scope_reply)
    g.add_node("retrieve", do_retrieve)
    g.add_node("glossary_answer", glossary_answer)
    g.add_node("generate", generate)
    g.add_node("validate", do_validate)
    g.add_node("execute", execute)
    g.add_node("summarize", summarize)
    g.add_node("remember", remember)

    g.set_entry_point("plan")
    g.add_conditional_edges("plan", lambda s: s["intent"], {
        "data_query": "retrieve", "glossary_lookup": "retrieve",
        "smalltalk": "scope_reply", "unsupported": "scope_reply"})
    g.add_conditional_edges("retrieve", lambda s: s["intent"], {
        "data_query": "generate", "glossary_lookup": "glossary_answer"})
    g.add_edge("generate", "validate")
    g.add_edge("validate", "execute")
    g.add_conditional_edges(
        "execute",
        lambda s: "repair" if s.get("exec_error") and s.get("repair_count", 0) <= 1
        else ("failed" if s.get("exec_error") else "ok"),
        {"repair": "generate", "ok": "summarize", "failed": "remember"})
    g.add_edge("scope_reply", "remember")
    g.add_edge("glossary_answer", "remember")
    g.add_edge("summarize", "remember")
    g.add_edge("remember", END)
    return g.compile(checkpointer=_CHECKPOINTER)


from langgraph.checkpoint.memory import InMemorySaver

_CHECKPOINTER = InMemorySaver()
