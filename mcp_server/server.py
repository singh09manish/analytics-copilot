"""MCP tool server for the Analytics Copilot warehouse (defense layer 2).

Run standalone (stdio):  uv run python ../mcp_server/server.py
Claude Desktop config:   command=<repo>/backend/.venv/bin/python, args=[<this file>]
"""
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "backend" / "src"))

# NOTE: the installed `mcp` package (2.0.0) renamed `mcp.server.fastmcp.FastMCP`
# to `mcp.server.mcpserver.MCPServer`; the decorator/run API is unchanged, so we
# alias it back to the familiar name.
from mcp.server.mcpserver import MCPServer as FastMCP

from copilot.sql_guard import SqlGuardError, validate

SIDE_EFFECT_PATTERN = re.compile(r"SYSTEM\$|GET_DDL\s*\(", re.IGNORECASE)

mcp = FastMCP("analytics-copilot-snowflake")


def _sf():
    if os.environ.get("COPILOT_FAKE_SNOWFLAKE") == "1":
        class _Fake:
            def run_query(self, sql, params=()):
                if "SCHEMA_CARDS" in sql or "GLOSSARY" in sql:
                    return (["CARD"], [("stub card",)])
                return (["MODEL"], [("TrueBeam",), ("Halcyon",)])

        return _Fake()

    from copilot.snowflake_client import SnowflakeClient

    return SnowflakeClient(role="COPILOT_APP_RO")


def _deny_side_effects(sql: str) -> None:
    if SIDE_EFFECT_PATTERN.search(sql):
        raise ValueError("side-effecting or metadata functions are not allowed")


def _run_query_impl(sql: str, sf) -> dict:
    _deny_side_effects(sql)
    try:
        safe = validate(sql)
    except SqlGuardError as e:
        raise ValueError(f"rejected by SQL guard: {e.reason}") from e
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
def search_glossary(question: str, k: int = 5) -> list[str]:
    """Business-glossary terms most relevant to the question."""
    from copilot.retrieval import retrieve

    return retrieve(question, _sf(), k_cards=0, k_terms=k).glossary


if __name__ == "__main__":
    mcp.run()
