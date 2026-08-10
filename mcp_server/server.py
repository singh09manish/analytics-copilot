"""MCP tool server for the Analytics Copilot warehouse (defense layer 2).

Run standalone (stdio):  uv run python ../mcp_server/server.py
Claude Desktop config:   command=<repo>/backend/.venv/bin/python, args=[<this file>]
"""
import os
import sys
from pathlib import Path
from typing import Annotated

sys.path.insert(0, str(Path(__file__).parents[1] / "backend" / "src"))

# NOTE: the installed `mcp` package (2.0.0) renamed `mcp.server.fastmcp.FastMCP`
# to `mcp.server.mcpserver.MCPServer`; the decorator/run API is unchanged, so we
# alias it back to the familiar name.
from mcp.server.mcpserver import MCPServer as FastMCP
from pydantic import Field

from copilot.sql_guard import SqlGuardError, deny_side_effects, validate

mcp = FastMCP("analytics-copilot-snowflake")

# Roles map exactly, per the plan's global constraint: JWT analyst -> COPILOT_APP_RO
# (masked), admin -> COPILOT_ADMIN (unmasked). Snowflake's masking policies CASE on
# CURRENT_ROLE(), so query *execution* must run under the caller's actual role, not
# always the RO role -- this allowlist is the last line of defense against that
# (defense in depth: the API is also expected to only ever send one of these two
# strings, never a client-supplied value).
_ALLOWED_ROLES = frozenset({"COPILOT_APP_RO", "COPILOT_ADMIN"})

_sf_clients: dict[str, object] = {}  # module-level cache: one SnowflakeClient/fake PER role, for the process lifetime


class _FakeSnowflake:
    """In-process stand-in for hermetic testing (COPILOT_FAKE_SNOWFLAKE=1).

    Mirrors backend/tests/conftest.py's FakeSnowflake shapes closely enough that
    all four tools (run_query, list_tables, describe_table, search_glossary) --
    including retrieval's vector-then-keyword-fallback path -- run without error.
    """

    def __init__(self, role: str = "COPILOT_APP_RO"):
        # Recorded (not used to vary query results) so tests can prove the right
        # role reached the right cached client without a real Snowflake connection.
        self.role = role

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


def _sf(role: str = "COPILOT_APP_RO"):
    """Lazily build, then reuse, one SnowflakeClient (or fake) per allowlisted role.

    Mirrors backend/src/copilot/api/main.py's app.state.sf_ro/sf_admin caching -- a
    stdio server is long-lived, so building a fresh client (and Snowflake session)
    per tool call would leak one session per call. `role` is never passed through
    to SnowflakeClient unvalidated: anything outside _ALLOWED_ROLES is rejected here
    rather than reaching Snowflake as an arbitrary role string.
    """
    if role not in _ALLOWED_ROLES:
        raise ValueError(
            f"role must be one of {sorted(_ALLOWED_ROLES)}, got {role!r}")
    if role not in _sf_clients:
        if os.environ.get("COPILOT_FAKE_SNOWFLAKE") == "1":
            _sf_clients[role] = _FakeSnowflake(role)
        else:
            from copilot.snowflake_client import SnowflakeClient

            _sf_clients[role] = SnowflakeClient(role=role)
    return _sf_clients[role]


def _deny_side_effects(sql: str) -> None:
    """Reject side-effecting/metadata scalar functions (GET_DDL, SYSTEM$...).

    The AST-walking implementation lives in copilot.sql_guard so layer 1 and layer 2
    enforce the identical rule from one piece of code (sql_guard.validate() runs it
    inline). Re-running it here is deliberate defense in depth: this server is a
    stdio tool server and will re-validate SQL handed to it by any MCP client,
    including ones that never went through the app's layer 1.
    """
    try:
        deny_side_effects(sql)
    except SqlGuardError as e:
        raise ValueError(e.reason) from e


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
def run_query(sql: str, role: str = "COPILOT_APP_RO") -> dict:
    """Execute one read-only SELECT against the GOLD schema. Validated server-side.

    `role` selects which Snowflake role/session runs the query (COPILOT_APP_RO or
    COPILOT_ADMIN only -- anything else is rejected) so masking policies that CASE
    on CURRENT_ROLE() are enforced for the caller's actual authenticated role.
    """
    return _run_query_impl(sql, _sf(role))


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
