from copilot.agent.pipeline import answer_question
from tests.conftest import FakeProvider, FakeSnowflake


def test_happy_path_returns_answer_sql_rows():
    r = answer_question("Which models do we have?", FakeProvider(), FakeSnowflake())
    assert r.error_type is None
    assert r.answer == "Here you go."
    assert "LIMIT 1000" in r.sql
    assert r.rows == [["TrueBeam"], ["Halcyon"]]
    assert r.tokens_in == 20


def test_guard_rejection_is_graceful():
    r = answer_question("drop it", FakeProvider(sql="DROP TABLE GOLD.DIM_MACHINE"),
                        FakeSnowflake())
    assert r.error_type == "validation"
    assert "safety rules" in r.answer


def test_snowflake_error_is_graceful():
    sf = FakeSnowflake()
    sf.fail = True
    r = answer_question("Which models?", FakeProvider(), sf)
    assert r.error_type == "snowflake"
    assert r.sql is not None


def test_total_snowflake_outage_is_graceful():
    class DeadSnowflake:
        def run_query(self, sql, params=()):
            raise RuntimeError("connection refused")

    r = answer_question("Which models?", FakeProvider(), DeadSnowflake())
    assert r.error_type == "snowflake"
    assert "warehouse" in r.answer.lower()


def test_summarize_failure_keeps_data():
    class SummarizeFails(FakeProvider):
        def text(self, system, user, max_tokens=1000):
            raise RuntimeError("api overloaded")

    r = answer_question("Which models?", SummarizeFails(), FakeSnowflake())
    assert r.error_type == "llm"
    assert r.rows == [["TrueBeam"], ["Halcyon"]]
    assert r.sql is not None
