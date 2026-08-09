"""MCP tool server for the Analytics Copilot warehouse (defense layer 2).

Run standalone (stdio):  uv run python ../mcp_server/server.py
Claude Desktop config:   command=<repo>/backend/.venv/bin/python, args=[<this file>]
"""
import os
import sys
from pathlib import Path
from typing import Annotated

sys.path.insert(0, str(Path(__file__).parents[1] / "backend" / "src"))

import sqlglot

# NOTE: the installed `mcp` package (2.0.0) renamed `mcp.server.fastmcp.FastMCP`
# to `mcp.server.mcpserver.MCPServer`; the decorator/run API is unchanged, so we
# alias it back to the familiar name.
from mcp.server.mcpserver import MCPServer as FastMCP
from pydantic import Field
from sqlglot import expressions as exp

from copilot.sql_guard import SqlGuardError, validate

mcp = FastMCP("analytics-copilot-snowflake")

_sf_client = None  # module-level cache: one SnowflakeClient/fake for the process lifetime


class _FakeSnowflake:
    """In-process stand-in for hermetic testing (COPILOT_FAKE_SNOWFLAKE=1).

    Mirrors backend/tests/conftest.py's FakeSnowflake shapes closely enough that
    all four tools (run_query, list_tables, describe_table, search_glossary) --
    including retrieval's vector-then-keyword-fallback path -- run without error.
    """

    def run_query(self, sql: str, params: tuple = ()):
        if "VECTOR_COSINE_SIMILARITY" in sql:
            raise RuntimeError("no cortex in fake-snowflake mode")
        if "WHERE UPPER(table_name)" in sql:
            return (["CARD"], [("stub card for GOLD.DIM_MACHINE",)])
        if "SCHEMA_CARDS" in sql:
            return (["TABLE_NAME", "CARD"],
                    [("GOLD.DIM_MACHINE", "stub card for GOLD.DIM_MACHINE")])
        if "GLOSSARY" in sql:
            return (["TERM", "DEFINITION"], [("MTTR", "mean time to repair")])
        return (["MODEL"], [("TrueBeam",), ("Halcyon",)])


def _sf():
    """Lazily build, then reuse, one SnowflakeClient (or fake) for this process.

    Mirrors backend/src/copilot/api/main.py's app.state.sf_ro caching -- a stdio
    server is long-lived, so building a fresh client (and Snowflake session) per
    tool call would leak one session per call.
    """
    global _sf_client
    if _sf_client is None:
        if os.environ.get("COPILOT_FAKE_SNOWFLAKE") == "1":
            _sf_client = _FakeSnowflake()
        else:
            from copilot.snowflake_client import SnowflakeClient

            _sf_client = SnowflakeClient(role="COPILOT_APP_RO")
    return _sf_client


def _deny_side_effects(sql: str) -> None:
    """Reject side-effecting/metadata scalar functions (GET_DDL, SYSTEM$...).

    Walks the PARSED tree of `sql` -- which must already be the guard-normalized
    output of sql_guard.validate(), not raw user input -- rather than regexing raw
    text. A regex over raw text is bypassable: sqlglot relocates comments (e.g.
    `GET_DDL/*x*/(...)` or `GET_DDL -- x\\n(...)`) when it re-serializes the guard's
    output, and it resolves double-quoted identifiers like `"GET_DDL"(...)` to the
    same builtin Snowflake would call -- both defeat a raw-text regex while still
    reaching Snowflake as a call to the denied function. Parsing catches all three
    forms because they all produce an exp.Anonymous/exp.Func node named GET_DDL,
    and it can't be fooled by a string literal that merely contains the text
    "GET_DDL(" (that parses to exp.Literal, not a function call).
    """
    tree = sqlglot.parse_one(sql, read="snowflake")
    for node in tree.walk():
        if isinstance(node, (exp.Anonymous, exp.Func)):
            name = (node.name or "").upper()
            if name == "GET_DDL" or name.startswith("SYSTEM$"):
                raise ValueError("side-effecting or metadata functions are not allowed")


def _run_query_impl(sql: str, sf) -> dict:
    try:
        safe = validate(sql)
    except SqlGuardError as e:
        raise ValueError(f"rejected by SQL guard: {e.reason}") from e
    _deny_side_effects(safe)
    columns, rows = sf.run_query(safe)
    return {"columns": list(columns), "rows": [list(r) for r in rows[:1000]]}


def _describe_table_impl(table_name: str, sf) -> str:
    _, rows = sf.run_query(
        "SELECT card FROM MEDTECH_ANALYTICS.COPILOT.SCHEMA_CARDS "
        "WHERE UPPER(table_name) = UPPER(%s)", (table_name,))
    return rows[0][0] if rows else f"No schema card found for {table_name}"


@mcp.tool()
def run_query(sql: str) -> dict:
    """Execute one read-only SELECT against GOLD/COPILOT. Validated server-side."""
    return _run_query_impl(sql, _sf())


@mcp.tool()
def list_tables() -> list[dict]:
    """List queryable gold tables with one-line summaries."""
    _, rows = _sf().run_query(
        "SELECT table_name, card FROM MEDTECH_ANALYTICS.COPILOT.SCHEMA_CARDS")
    return [{"table": r[0], "summary": r[1].split("\n")[0][:120]} for r in rows]


@mcp.tool()
def describe_table(table_name: str) -> str:
    """Full schema card for one table (columns, semantics, sample questions)."""
    return _describe_table_impl(table_name, _sf())


@mcp.tool()
def search_glossary(question: str, k: Annotated[int, Field(ge=1, le=50)] = 5) -> list[str]:
    """Business-glossary terms most relevant to the question."""
    from copilot.retrieval import retrieve

    return retrieve(question, _sf(), k_cards=0, k_terms=k).glossary


if __name__ == "__main__":
    mcp.run()
