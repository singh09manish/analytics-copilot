"""Round-trip through a real stdio MCP server subprocess with Snowflake faked out.

The server subprocess imports copilot.snowflake_client for real, so we point it at a
stub via env: COPILOT_FAKE_SNOWFLAKE=1 makes server.py use an in-process fake.
"""
import pytest

from copilot.mcp_client import McpError, McpExecutor


@pytest.fixture(scope="module")
def executor(monkeypatch_module_env):
    ex = McpExecutor()
    yield ex
    ex.close()


@pytest.fixture(scope="module")
def monkeypatch_module_env():
    import os

    os.environ["COPILOT_FAKE_SNOWFLAKE"] = "1"
    yield
    os.environ.pop("COPILOT_FAKE_SNOWFLAKE", None)


def test_round_trip_query(executor):
    cols, rows = executor.run_query("SELECT model FROM GOLD.DIM_MACHINE")
    assert cols == ["MODEL"] and rows == [["TrueBeam"], ["Halcyon"]]


def test_round_trip_guard_rejection(executor):
    with pytest.raises(McpError, match="rejected by SQL guard"):
        executor.run_query("DROP TABLE GOLD.DIM_MACHINE")
