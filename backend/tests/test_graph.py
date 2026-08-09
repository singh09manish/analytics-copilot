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


def test_different_conversation_ids_get_clean_state():
    """conv-A's history must not leak into conv-B's prompts (thread isolation)."""
    p = PlanningFakeProvider()
    sf = FakeSnowflake()
    answer_question("Which models?", p, sf, conversation_id="conv-A")
    answer_question("something else entirely", p, sf, conversation_id="conv-B")
    sql_user_inputs = [u for n, u in p.structured_calls if n == "SqlDraft"]
    assert "Which models?" not in sql_user_inputs[-1]


def test_executor_injection_used_instead_of_snowflake():
    """Task 6's contract: a supplied executor runs the SQL, not sf.run_query."""
    captured = []

    def fake_executor(sql):
        captured.append(sql)
        return (["MODEL"], [("TrueBeam",)])

    sf = FakeSnowflake()
    r = answer_question("Which models?", PlanningFakeProvider(), sf, executor=fake_executor)
    assert r.error_type is None
    assert captured and "GOLD.DIM_MACHINE" in captured[-1]
    assert r.rows == [["TrueBeam"]]
    assert not any("DIM_MACHINE" in q for q in sf.queries)  # never executed via sf.run_query


def test_repair_prompt_carries_execution_error_text():
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

    p = PlanningFakeProvider()
    sf = FlakySnowflake()
    answer_question("Which models?", p, sf)
    sql_prompts = [u for n, u in p.structured_calls if n == "SqlDraft"]
    assert len(sql_prompts) == 2
    assert "invalid identifier" in sql_prompts[-1]


def test_anonymous_requests_dont_leak_into_checkpointer():
    """conversation_id=None must not mint a permanent entry in the shared InMemorySaver."""
    from copilot.agent import graph as agent_graph

    before = len(agent_graph._CHECKPOINTER.storage)
    for _ in range(5):
        answer_question("Which models?", PlanningFakeProvider(), FakeSnowflake())
    assert len(agent_graph._CHECKPOINTER.storage) == before


def test_plan_llm_failure_is_llm_not_snowflake():
    """A non-ValueError provider failure (e.g. rate limit) must not be misclassified
    as a warehouse outage — it would poison error_type in Task 7's REQUEST_LOG."""
    class RateLimitedProvider(PlanningFakeProvider):
        def structured(self, system, user, schema, max_tokens=1500):
            raise RuntimeError("rate limited")

    r = answer_question("Which models?", RateLimitedProvider(), FakeSnowflake())
    assert r.error_type == "llm"


def test_generate_llm_failure_is_llm_not_snowflake():
    class FlakyGenerateProvider(PlanningFakeProvider):
        def structured(self, system, user, schema, max_tokens=1500):
            self.structured_calls.append((schema.__name__, user))
            if schema is QueryPlan:
                return LLMResult(value=QueryPlan(intent="data_query", entities=[]),
                                 tokens_in=5, tokens_out=2)
            raise RuntimeError("api overloaded")

    r = answer_question("Which models?", FlakyGenerateProvider(), FakeSnowflake())
    assert r.error_type == "llm"
    assert r.sql is None


def test_glossary_llm_failure_is_llm_not_snowflake():
    class FlakyGlossaryProvider(PlanningFakeProvider):
        def __init__(self):
            super().__init__(intent="glossary_lookup")

        def text(self, system, user, max_tokens=1000):
            raise RuntimeError("api overloaded")

    r = answer_question("What does MTTR mean?", FlakyGlossaryProvider(), FakeSnowflake())
    assert r.error_type == "llm"
