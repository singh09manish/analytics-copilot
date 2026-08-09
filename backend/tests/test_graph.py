from copilot.agent.pipeline import answer_question
from copilot.llm.provider import LLMResult
from copilot.llm.schemas import QueryPlan
from tests.conftest import FakeProvider, FakeSnowflake


class PlanningFakeProvider(FakeProvider):
    """FakeProvider that also answers plan-node calls; scriptable intent."""

    def __init__(self, intent="data_query", **kw):
        super().__init__(**kw)
        self.intent = intent
        self.structured_calls = []

    def structured(self, system, user, schema, max_tokens=1500):
        self.structured_calls.append((schema.__name__, user))
        if schema is QueryPlan:
            return LLMResult(value=QueryPlan(intent=self.intent, entities=[]),
                             tokens_in=5, tokens_out=2)
        return super().structured(system, user, schema, max_tokens)


def test_data_query_end_to_end():
    r = answer_question("Which models?", PlanningFakeProvider(), FakeSnowflake())
    assert r.error_type is None
    assert r.intent == "data_query"
    assert "LIMIT 1000" in r.sql
    assert r.rows == [["TrueBeam"], ["Halcyon"]]


def test_smalltalk_never_generates_sql():
    p = PlanningFakeProvider(intent="smalltalk")
    r = answer_question("hey there!", p, FakeSnowflake())
    assert r.sql is None and r.intent == "smalltalk"
    assert not any(n == "SqlDraft" for n, _ in p.structured_calls)


def test_glossary_lookup_answers_without_sql():
    r = answer_question("What does MTTR mean?",
                        PlanningFakeProvider(intent="glossary_lookup"), FakeSnowflake())
    assert r.sql is None and r.intent == "glossary_lookup"
    assert r.answer  # text answer produced from glossary context


def test_repair_loop_retries_failed_sql_once():
    class FlakySnowflake(FakeSnowflake):
        def __init__(self):
            super().__init__()
            self.exec_attempts = 0

        def run_query(self, sql, params=()):
            if "GOLD.DIM_MACHINE" in sql and "VECTOR" not in sql:
                self.exec_attempts += 1
                if self.exec_attempts == 1:
                    raise RuntimeError("SQL compilation error: invalid identifier 'MODELL'")
            return super().run_query(sql, params)

    sf = FlakySnowflake()
    r = answer_question("Which models?", PlanningFakeProvider(), sf)
    assert sf.exec_attempts == 2  # failed once, repaired, succeeded
    assert r.error_type is None


def test_multi_turn_memory_feeds_history():
    p = PlanningFakeProvider()
    sf = FakeSnowflake()
    answer_question("Which models?", p, sf, conversation_id="c1")
    answer_question("now break that down by region", p, sf, conversation_id="c1")
    sql_user_inputs = [u for n, u in p.structured_calls if n == "SqlDraft"]
    assert "Which models?" in sql_user_inputs[-1]  # history reached the SQL prompt


def test_never_raises_on_plan_failure():
    class ExplodingProvider(PlanningFakeProvider):
        def structured(self, system, user, schema, max_tokens=1500):
            raise ValueError("LLM output schema-invalid twice for QueryPlan")

    r = answer_question("Which models?", ExplodingProvider(), FakeSnowflake())
    assert r.error_type == "llm"
