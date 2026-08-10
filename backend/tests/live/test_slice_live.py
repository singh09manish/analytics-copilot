import pytest

from copilot.agent.pipeline import answer_question
from copilot.llm.provider import AnthropicProvider
from copilot.snowflake_client import SnowflakeClient

pytestmark = pytest.mark.live


def test_slice_end_to_end():
    r = answer_question(
        "Which 5 treatment centers had the most machine downtime hours in the last 90 days?",
        AnthropicProvider(), SnowflakeClient(role="COPILOT_APP_RO"))
    assert r.error_type is None, f"{r.error_type}: {r.answer}"
    assert r.sql and "FACT_MACHINE_UTILIZATION" in r.sql.upper()
    assert len(r.rows) == 5
    assert any(ch.isdigit() for ch in r.answer)
    print("\nSQL:\n", r.sql, "\nANSWER:\n", r.answer)


def test_multi_turn_followup_live():
    """Multi-turn memory only activates when conversation_id is passed; the
    checkpointer keyed by thread_id lets the second turn's SQL react to the
    first turn's context ("now only the Active ones" -> filter on STATUS)."""
    provider = AnthropicProvider()
    sf = SnowflakeClient(role="COPILOT_APP_RO")
    r1 = answer_question("How many machines do we have per model?", provider, sf,
                         conversation_id="live-mt-1")
    assert r1.error_type is None and r1.intent == "data_query"
    r2 = answer_question("now only the Active ones", provider, sf,
                         conversation_id="live-mt-1")
    assert r2.error_type is None
    assert "STATUS" in (r2.sql or "").upper() or "ACTIVE" in (r2.sql or "").upper()


def test_mcp_executor_live():
    """Query execution through the MCP tool server (layer-2 SQL validation)
    rather than a direct SnowflakeClient call."""
    from copilot.mcp_client import McpExecutor

    ex = McpExecutor()
    try:
        _cols, rows = ex.run_query(
            "SELECT COUNT(*) AS n FROM GOLD.FACT_MACHINE_UTILIZATION")
        assert rows[0][0] == 146000
    finally:
        ex.close()


def test_mcp_rbac_masking_live():
    """The heart of the demo: the same query through the MCP path returns
    masked contact emails as COPILOT_APP_RO and unmasked ones as
    COPILOT_ADMIN, because GOLD.DIM_TREATMENT_CENTER's masking policy CASEs
    on CURRENT_ROLE() (see warehouse/dbt/models/gold/dim_treatment_center.sql
    for the exact '***MASKED***' literal asserted below)."""
    from copilot.mcp_client import McpExecutor

    ex = McpExecutor()
    try:
        sql = ("SELECT center_name, contact_email FROM GOLD.DIM_TREATMENT_CENTER "
               "ORDER BY center_id LIMIT 5")
        _, ro_rows = ex.run_query(sql, role="COPILOT_APP_RO")
        _, admin_rows = ex.run_query(sql, role="COPILOT_ADMIN")
        assert ro_rows and admin_rows
        ro_emails = [row[1] for row in ro_rows]
        admin_emails = [row[1] for row in admin_rows]
        assert all(e == "***MASKED***" for e in ro_emails)
        assert all(e != "***MASKED***" and "@" in e for e in admin_emails)
        assert admin_emails != ro_emails
    finally:
        ex.close()


def test_mcp_role_allowlist_rejects_out_of_scope_role_live():
    """The MCP server's role allowlist ({COPILOT_APP_RO, COPILOT_ADMIN}) is the
    last line of defense against a role that was never supposed to reach
    Snowflake through this path -- it must be rejected server-side, not
    silently used to open a session."""
    from copilot.mcp_client import McpError, McpExecutor

    ex = McpExecutor()
    try:
        with pytest.raises(McpError, match="role must be one of"):
            ex.run_query("SELECT COUNT(*) AS n FROM GOLD.DIM_MACHINE", role="ACCOUNTADMIN")
    finally:
        ex.close()
