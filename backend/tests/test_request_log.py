import logging

from copilot.agent.pipeline import ChatResponse
from copilot.request_log import COLUMNS, INSERT_SQL, log_request
from tests.conftest import FakeSnowflake


def test_log_request_inserts_row():
    sf = FakeSnowflake()
    r = ChatResponse(answer="ok", intent="data_query", request_id="rid1",
                     sql="SELECT 1", tokens_in=10, tokens_out=5, retrieval_ms=99)
    log_request(sf, request_id="rid1", conversation_id="c1", user_role="analyst",
                question="q?", response=r, e2e_ms=1234)
    assert any("INSERT INTO MEDTECH_ANALYTICS.COPILOT.REQUEST_LOG" in q
               for q in sf.queries)


def test_log_request_never_raises():
    class Dead:
        def run_query(self, sql, params=()):
            raise RuntimeError("writer down")

    r = ChatResponse(answer="ok", request_id="rid2")
    log_request(Dead(), request_id="rid2", conversation_id=None, user_role="analyst",
                question="q?", response=r, e2e_ms=1)  # must not raise


# --- Final review, Important finding I7: the hermetic suite could not catch a broken
# INSERT. FakeSnowflake accepted `params` and threw it away, and the only assertion
# was that the SQL mentioned REQUEST_LOG -- so a wrong param count would have failed
# only against live Snowflake, where log_request swallows it and logs nothing.


def _column_count(sql: str) -> int:
    return len(sql[sql.index("(") + 1:sql.index(")")].split(","))


def test_request_log_insert_column_placeholder_and_param_counts_agree():
    sf = FakeSnowflake()
    r = ChatResponse(answer="ok", intent="data_query", request_id="rid1", sql="SELECT 1")
    log_request(sf, request_id="rid1", conversation_id="c1", user_role="analyst",
                question="q?", response=r, e2e_ms=1234)

    sql, params = sf.calls[-1]
    assert _column_count(sql) == len(COLUMNS) == 13  # fixed by warehouse/bootstrap.sql
    assert sql.count("%s") == len(COLUMNS)
    assert len(params) == len(COLUMNS)


def test_request_log_columns_match_the_fixed_ops_table_ddl():
    """REQUEST_LOG has 14 columns; created_at defaults, so the INSERT names 13."""
    assert COLUMNS == (
        "request_id", "conversation_id", "user_role", "question", "intent",
        "sql_text", "status", "error_type", "e2e_ms", "retrieval_ms",
        "tokens_in", "tokens_out", "prompt_version")
    assert INSERT_SQL.count("%s") == 13


def test_log_request_failure_is_logged_not_silent(caplog):
    class Dead:
        def run_query(self, sql, params=()):
            raise RuntimeError("writer down")

    r = ChatResponse(answer="ok", request_id="rid3")
    with caplog.at_level(logging.WARNING):
        log_request(Dead(), request_id="rid3", conversation_id=None,
                    user_role="analyst", question="q?", response=r, e2e_ms=1)
    assert "REQUEST_LOG" in caplog.text
    assert "rid3" in caplog.text
