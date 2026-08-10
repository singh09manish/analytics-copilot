import importlib.util
import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).parents[2] / "mcp_server" / "server.py"

spec = importlib.util.spec_from_file_location("mcp_srv", SERVER)
mcp_srv = importlib.util.module_from_spec(spec)
sys.modules["mcp_srv"] = mcp_srv
spec.loader.exec_module(mcp_srv)

from tests.conftest import FakeSnowflake


class CardFake(FakeSnowflake):
    def run_query(self, sql, params=()):
        if "WHERE UPPER(table_name)" in sql:
            return (["CARD"], [("GOLD.DIM_MACHINE - one row per installed machine",)])
        return super().run_query(sql, params)


@pytest.fixture
def fake_snowflake_env(monkeypatch):
    """Route the module's own _sf() singleton to the in-process fake, hermetically.

    Uses monkeypatch (auto-reverted at teardown) rather than mutating
    os.environ directly at module scope. NOTE: _sf() caches its client in a
    module-level global once built, so once any test in this session has
    exercised it under this fixture, later calls reuse that same fake instance
    regardless of the env var -- which is fine, since nothing in this file ever
    wants a real connection.
    """
    monkeypatch.setenv("COPILOT_FAKE_SNOWFLAKE", "1")


def test_run_query_revalidates_and_executes():
    out = mcp_srv._run_query_impl("SELECT model FROM GOLD.DIM_MACHINE", FakeSnowflake())
    assert out["columns"] == ["MODEL"]
    assert out["rows"] == [["TrueBeam"], ["Halcyon"]]


def test_run_query_rejects_bad_sql_at_layer2():
    with pytest.raises(ValueError, match="rejected"):
        mcp_srv._run_query_impl("DROP TABLE GOLD.DIM_MACHINE", FakeSnowflake())


@pytest.mark.parametrize("evil", [
    "SELECT SYSTEM$CANCEL_ALL_QUERIES(123)",
    "SELECT GET_DDL('TABLE', 'GOLD.DIM_MACHINE')",
])
def test_side_effecting_scalars_denied(evil):
    with pytest.raises(ValueError, match="side-effect"):
        mcp_srv._run_query_impl(evil, FakeSnowflake())


@pytest.mark.parametrize("evil", [
    "SELECT GET_DDL/*x*/('TABLE','GOLD.DIM_MACHINE')",
    "SELECT GET_DDL -- x\n('TABLE','GOLD.DIM_MACHINE')",
    'SELECT "GET_DDL"(\'TABLE\',\'GOLD.DIM_MACHINE\')',
])
def test_obfuscated_side_effecting_calls_still_denied(evil):
    """Regression for the Critical finding: sql_guard.validate() re-serializes SQL,
    which relocates comments and can leave a denylist regex run on the ORIGINAL
    text no longer matching the text Snowflake actually receives -- and a
    double-quoted identifier form evades a raw regex on both original and
    normalized text either way. _deny_side_effects must walk the parsed tree of
    the guard's own output, not regex raw text, to catch all three forms."""
    with pytest.raises(ValueError, match="side-effect"):
        mcp_srv._run_query_impl(evil, FakeSnowflake())


def test_clean_select_passes_deny_check():
    safe = mcp_srv.validate("SELECT model FROM GOLD.DIM_MACHINE")
    mcp_srv._deny_side_effects(safe)  # must not raise


def test_describe_table_returns_card():
    card = mcp_srv._describe_table_impl("GOLD.DIM_MACHINE", CardFake())
    assert "machine" in card.lower()


def test_list_tables_runs_under_fake_hook(fake_snowflake_env):
    out = mcp_srv.list_tables()
    assert out == [{"table": "GOLD.DIM_MACHINE", "summary": "stub card for GOLD.DIM_MACHINE"}]


def test_describe_table_runs_under_fake_hook(fake_snowflake_env):
    out = mcp_srv.describe_table("GOLD.DIM_MACHINE")
    assert out == "stub card for GOLD.DIM_MACHINE"


def test_run_query_runs_under_fake_hook(fake_snowflake_env):
    out = mcp_srv.run_query("SELECT model FROM GOLD.DIM_MACHINE")
    assert out["columns"] == ["MODEL"]
    assert out["rows"] == [["TrueBeam"], ["Halcyon"]]


def test_search_glossary_runs_under_fake_hook(fake_snowflake_env):
    out = mcp_srv.search_glossary("what is MTTR?", k=3)
    assert out == ["MTTR: mean time to repair"]


# --- Task 7 follow-up review, Finding 1: query EXECUTION must be role-aware so
# Snowflake's CURRENT_ROLE()-based masking applies for admins too, not just RO.


def test_sf_caches_one_client_per_allowlisted_role(fake_snowflake_env):
    mcp_srv._sf_clients.clear()
    ro = mcp_srv._sf("COPILOT_APP_RO")
    admin = mcp_srv._sf("COPILOT_ADMIN")
    assert ro.role == "COPILOT_APP_RO"
    assert admin.role == "COPILOT_ADMIN"
    assert ro is not admin
    assert mcp_srv._sf("COPILOT_APP_RO") is ro  # cached, not rebuilt
    assert mcp_srv._sf("COPILOT_ADMIN") is admin


def test_sf_rejects_role_outside_allowlist(fake_snowflake_env):
    mcp_srv._sf_clients.clear()
    with pytest.raises(ValueError, match="role must be one of"):
        mcp_srv._sf("PUBLIC")
    assert mcp_srv._sf_clients == {}  # never cached an unvalidated role


def test_sf_never_passes_arbitrary_role_string_through(fake_snowflake_env, monkeypatch):
    """The allowlist check must run BEFORE anything reaches a real SnowflakeClient
    constructor -- assert this by making a real-mode SnowflakeClient() call fail
    loudly if it's ever invoked with the malicious string."""
    monkeypatch.delenv("COPILOT_FAKE_SNOWFLAKE", raising=False)
    mcp_srv._sf_clients.clear()
    with pytest.raises(ValueError, match="role must be one of"):
        mcp_srv._sf("'; DROP ROLE ACCOUNTADMIN; --")
    assert mcp_srv._sf_clients == {}


def test_run_query_tool_forwards_role_to_sf(fake_snowflake_env):
    mcp_srv._sf_clients.clear()
    mcp_srv.run_query("SELECT model FROM GOLD.DIM_MACHINE", role="COPILOT_ADMIN")
    assert "COPILOT_ADMIN" in mcp_srv._sf_clients
    assert "COPILOT_APP_RO" not in mcp_srv._sf_clients


def test_run_query_tool_defaults_to_copilot_app_ro(fake_snowflake_env):
    mcp_srv._sf_clients.clear()
    mcp_srv.run_query("SELECT model FROM GOLD.DIM_MACHINE")
    assert "COPILOT_APP_RO" in mcp_srv._sf_clients


def test_run_query_tool_rejects_out_of_allowlist_role(fake_snowflake_env):
    mcp_srv._sf_clients.clear()
    with pytest.raises(ValueError, match="role must be one of"):
        mcp_srv.run_query("SELECT model FROM GOLD.DIM_MACHINE", role="SOME_OTHER_ROLE")
