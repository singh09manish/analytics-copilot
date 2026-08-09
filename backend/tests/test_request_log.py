from copilot.agent.pipeline import ChatResponse
from copilot.request_log import log_request
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
