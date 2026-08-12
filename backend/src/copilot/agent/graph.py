"""LangGraph agent: plan -> (route) -> retrieve -> generate -> validate -> execute
-> summarize, with one repair cycle and per-conversation memory."""
import csv
import io
import logging
from typing import TypedDict

from langgraph.graph import END, StateGraph

from copilot.agent import prompts
from copilot.agent.checkpointer import BoundedInMemorySaver
from copilot.llm.schemas import QueryPlan, SqlDraft
from copilot.retrieval import RetrievedContext, retrieve
from copilot.sql_guard import SqlGuardError, validate

logger = logging.getLogger(__name__)

# Process-global conversation memory, shared by every graph this module builds and
# keyed by thread_id. Bounded (see checkpointer.py): a plain InMemorySaver retained
# every super-step's channel values -- including result rows -- forever.
_CHECKPOINTER = BoundedInMemorySaver()

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
    retrieval_mode: str  # "vector" | "keyword" -- see retrieval.RetrievedContext.mode


def _provider_label(provider) -> str:
    """Provider class plus the model id it is pinned to.

    Every LLM-failure log line carries this so an operator can tell from the logs
    alone which model the *deployed* task actually called -- APP_MODEL is a plain
    task-definition environment variable, so it can differ from a developer's .env
    without anything else in the system noticing.
    """
    return f"{type(provider).__name__}(model={getattr(provider, '_model', 'unknown')})"


def _rows_as_csv(columns: list, rows: list) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(columns)
    w.writerows(rows[:MAX_SUMMARY_ROWS])
    return buf.getvalue()


def build_graph(provider, sf, executor=None, use_memory: bool = True):
    # `executor or sf.run_query` would silently fall through to sf.run_query for any
    # falsy-but-valid executor object (e.g. a callable wrapper with __bool__ == False) —
    # compare against None explicitly so MCP-injected executors are never bypassed.
    run_sql = executor if executor is not None else sf.run_query

    def plan(state: AgentState) -> dict:
        user = prompts.user_with_history(state["question"], state.get("history", []))
        try:
            res = provider.structured(system=prompts.plan_system(), user=user,
                                      schema=QueryPlan, max_tokens=300)
        except Exception:  # LLM origin; classify as llm, never crash the graph
            # The user-facing string is deliberately vague; the log line is not. Without
            # this, a total LLM outage looks identical to a working app answering badly.
            logger.warning("plan node: LLM call failed via %s; answering with the "
                           "generic llm error.", _provider_label(provider), exc_info=True)
            return {"error_type": "llm",
                    "answer": "I couldn't process that request right now. Please try "
                              "again in a moment."}
        return {"intent": res.value.intent,
                "tokens_in": state.get("tokens_in", 0) + res.tokens_in,
                "tokens_out": state.get("tokens_out", 0) + res.tokens_out}

    def scope_reply(state: AgentState) -> dict:
        if state["intent"] == "smalltalk":
            return {"answer": "Hi! " + SCOPE_MESSAGE}
        return {"answer": SCOPE_MESSAGE}

    def do_retrieve(state: AgentState) -> dict:
        ctx = retrieve(state["question"], sf)
        return {"context": ctx.model_dump(), "retrieval_ms": ctx.retrieval_ms,
                "retrieval_mode": ctx.mode}

    def glossary_answer(state: AgentState) -> dict:
        ctx = RetrievedContext.model_validate(state["context"])
        glossary = "\n".join(f"- {g}" for g in ctx.glossary)
        try:
            res = provider.text(system=prompts.glossary_system(),
                                user=f"Glossary entries:\n{glossary}\n\n"
                                     f"Question: {state['question']}")
        except Exception:  # LLM origin; classify as llm, never crash the graph
            logger.warning("glossary_answer node: LLM call failed via %s; answering "
                           "with the generic llm error.",
                           _provider_label(provider), exc_info=True)
            return {"answer": "I couldn't look that term up right now. Please try "
                              "again in a moment.",
                    "error_type": "llm"}
        return {"answer": res.value,
                "tokens_in": state.get("tokens_in", 0) + res.tokens_in,
                "tokens_out": state.get("tokens_out", 0) + res.tokens_out}

    def generate(state: AgentState) -> dict:
        ctx = RetrievedContext.model_validate(state["context"])
        user = prompts.user_with_history(state["question"], state.get("history", []))
        if state.get("exec_error"):
            user += (f"\n\nYour previous SQL failed with this Snowflake error - "
                     f"fix it:\n{state['exec_error']}\nPrevious SQL:\n{state['draft_sql']}")
        try:
            res = provider.structured(system=prompts.sql_system(ctx), user=user,
                                      schema=SqlDraft)
        except Exception:  # LLM origin; classify as llm, never crash the graph
            logger.warning("generate node: LLM call failed via %s (repair_count=%s); "
                           "answering with the generic llm error.",
                           _provider_label(provider), state.get("repair_count", 0),
                           exc_info=True)
            return {"error_type": "llm",
                    "answer": "I couldn't turn that into a query. Try rephrasing with "
                              "the metric and time range you care about."}
        draft: SqlDraft = res.value
        return {"draft_sql": draft.sql, "assumptions": draft.assumptions,
                "tokens_in": state.get("tokens_in", 0) + res.tokens_in,
                "tokens_out": state.get("tokens_out", 0) + res.tokens_out}

    def do_validate(state: AgentState) -> dict:
        try:
            return {"safe_sql": validate(state["draft_sql"])}
        except SqlGuardError as e:
            logger.warning("validate node: SQL guard rejected the draft (reason=%s).",
                           e.reason, exc_info=True)
            # Guard rejections are not retried (repair is execute-failure-only); keep
            # the rejected draft_sql/tokens/intent/retrieval_ms already in state so
            # Task 7's ops log gets a full record instead of NULLs.
            return {"error_type": "validation",
                    "answer": f"I generated a query the safety rules rejected "
                              f"({e.reason}). Try rephrasing your question."}

    def execute(state: AgentState) -> dict:
        try:
            columns, rows = run_sql(state["safe_sql"])
            return {"columns": list(columns), "rows": [list(r) for r in rows],
                    "exec_error": ""}
        except Exception as e:  # routed to repair or graceful error
            # exec_error keeps only str(e)[:500] for the repair prompt; the traceback
            # is the only way to tell a bad-SQL failure from a warehouse/connection one.
            logger.warning("execute node: SQL execution failed (attempt %s).",
                           state.get("repair_count", 0) + 1, exc_info=True)
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
        except Exception:  # keep the data even if summarization fails
            logger.warning("summarize node: LLM call failed via %s; returning the rows "
                           "without a summary.", _provider_label(provider), exc_info=True)
            return {"answer": "I ran the query successfully but couldn't generate a "
                              "summary. The results are shown below.",
                    "error_type": "llm"}

    def remember(state: AgentState) -> dict:
        history = list(state.get("history", []))
        answer = state.get("answer") or ""
        if not answer:
            # A turn can reach here with no answer set (e.g. repair exhausted after
            # execute() failure). Record the failure reason instead of an empty string
            # so the next turn's "A: " line in user_with_history() isn't blank.
            fallback = state.get("exec_error") or state.get("error_type") or "no answer produced"
            answer = f"[failed: {fallback}]"
        history.append((state["question"], answer[:500]))
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
    g.add_conditional_edges(
        "plan", lambda s: "error" if s.get("error_type") else s["intent"],
        {"data_query": "retrieve", "glossary_lookup": "retrieve",
         "smalltalk": "scope_reply", "unsupported": "scope_reply",
         "error": "remember"})
    g.add_conditional_edges("retrieve", lambda s: s["intent"], {
        "data_query": "generate", "glossary_lookup": "glossary_answer"})
    g.add_conditional_edges(
        "generate", lambda s: "error" if s.get("error_type") else "validate",
        {"error": "remember", "validate": "validate"})
    g.add_conditional_edges(
        "validate", lambda s: "error" if s.get("error_type") else "execute",
        {"error": "remember", "execute": "execute"})
    g.add_conditional_edges(
        "execute",
        lambda s: "repair" if s.get("exec_error") and s.get("repair_count", 0) <= 1
        else ("failed" if s.get("exec_error") else "ok"),
        {"repair": "generate", "ok": "summarize", "failed": "remember"})
    g.add_edge("scope_reply", "remember")
    g.add_edge("glossary_answer", "remember")
    g.add_edge("summarize", "remember")
    g.add_edge("remember", END)
    # Anonymous (conversation_id=None) callers get a checkpointer-free compile so a
    # never-reused thread_id doesn't mint a permanent, never-freed entry in the
    # process-global InMemorySaver.
    return g.compile(checkpointer=_CHECKPOINTER if use_memory else None)
