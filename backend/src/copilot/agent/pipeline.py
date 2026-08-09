import csv
import io

from pydantic import BaseModel

from copilot.agent import prompts
from copilot.llm.provider import LLMProvider
from copilot.llm.schemas import SqlDraft
from copilot.retrieval import retrieve
from copilot.snowflake_client import SnowflakeClient
from copilot.sql_guard import SqlGuardError, validate

MAX_SUMMARY_ROWS = 50


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


def _rows_as_csv(columns: list[str], rows: list[tuple]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(columns)
    w.writerows(rows[:MAX_SUMMARY_ROWS])
    return buf.getvalue()


def answer_question(question: str, provider: LLMProvider, sf: SnowflakeClient) -> ChatResponse:
    tokens_in = tokens_out = 0
    context = retrieve(question, sf)
    try:
        draft_res = provider.structured(
            system=prompts.sql_system(context), user=question, schema=SqlDraft)
        draft: SqlDraft = draft_res.value
        tokens_in += draft_res.tokens_in
        tokens_out += draft_res.tokens_out
    except ValueError:
        return ChatResponse(
            answer="I couldn't turn that into a query. Try rephrasing with the metric "
                   "and time range you care about.",
            error_type="llm", retrieval_ms=context.retrieval_ms)
    try:
        safe_sql = validate(draft.sql)
    except SqlGuardError as e:
        return ChatResponse(
            answer=f"I generated a query the safety rules rejected ({e.reason}). "
                   "Try rephrasing your question.",
            sql=draft.sql, error_type="validation", retrieval_ms=context.retrieval_ms,
            tokens_in=tokens_in, tokens_out=tokens_out)
    try:
        columns, rows = sf.run_query(safe_sql)
    except Exception as e:  # noqa: BLE001  # snowflake errors -> graceful message
        return ChatResponse(
            answer="The query failed against the warehouse. This usually means I "
                   "misread the schema - try asking a bit differently.",
            sql=safe_sql, error_type="snowflake", retrieval_ms=context.retrieval_ms,
            assumptions=[str(e)[:200]], tokens_in=tokens_in, tokens_out=tokens_out)
    summary = provider.text(
        system=prompts.summarize_system(),
        user=f"Question: {question}\n\nSQL:\n{safe_sql}\n\nResults (CSV, first "
             f"{MAX_SUMMARY_ROWS} rows):\n{_rows_as_csv(columns, rows)}")
    tokens_in += summary.tokens_in
    tokens_out += summary.tokens_out
    return ChatResponse(
        answer=summary.value, sql=safe_sql, columns=columns,
        rows=[list(r) for r in rows[:200]], assumptions=draft.assumptions,
        retrieval_ms=context.retrieval_ms, tokens_in=tokens_in, tokens_out=tokens_out)
