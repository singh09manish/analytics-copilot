from typing import Literal

from pydantic import BaseModel, Field


class SqlDraft(BaseModel):
    sql: str = Field(description="One Snowflake SELECT statement, fully qualified tables")
    tables_used: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)


class QueryPlan(BaseModel):
    intent: Literal["data_query", "glossary_lookup", "smalltalk", "unsupported"]
    entities: list[str] = Field(default_factory=list)
