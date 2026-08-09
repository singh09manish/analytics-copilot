from pydantic import BaseModel

from copilot.llm.provider import LLMResult
from copilot.llm.schemas import SqlDraft


class FakeProvider:
    """Scriptable LLMProvider double."""

    def __init__(self, sql="SELECT model FROM GOLD.DIM_MACHINE", answer="Here you go."):
        self.sql = sql
        self.answer = answer

    def structured(self, system: str, user: str, schema: type[BaseModel],
                   max_tokens: int = 1500) -> LLMResult:
        return LLMResult(value=SqlDraft(sql=self.sql, tables_used=["GOLD.DIM_MACHINE"]),
                         tokens_in=10, tokens_out=5)

    def text(self, system: str, user: str, max_tokens: int = 1000) -> LLMResult:
        return LLMResult(value=self.answer, tokens_in=10, tokens_out=5)


class FakeSnowflake:
    """Returns canned rows; records queries. Raises if .fail is set."""

    def __init__(self):
        self.queries = []
        self.fail = False
        self.result = (["MODEL"], [("TrueBeam",), ("Halcyon",)])

    def run_query(self, sql: str, params: tuple = ()):
        self.queries.append(sql)
        if "VECTOR_COSINE_SIMILARITY" in sql:
            raise RuntimeError("no cortex in tests")
        if "SCHEMA_CARDS" in sql:
            return (["TABLE_NAME", "CARD"],
                    [("GOLD.DIM_MACHINE", "machines models installed"),
                     ("GOLD.FACT_MACHINE_UTILIZATION", "downtime uptime fractions")])
        if "GLOSSARY" in sql:
            return (["TERM", "DEFINITION"], [("MTTR", "mean repair"), ("fraction", "session")])
        if self.fail:
            raise RuntimeError("SQL compilation error: invalid identifier")
        return self.result
