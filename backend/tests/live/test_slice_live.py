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
