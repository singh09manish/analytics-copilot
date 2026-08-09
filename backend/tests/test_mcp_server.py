import importlib.util
import os
import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).parents[2] / "mcp_server" / "server.py"

# Hermetic: the server module can build a real SnowflakeClient inside _sf(), but
# nothing in this file calls _sf() (impls are called directly with an explicit
# `sf` fake) -- set the fake-Snowflake hook anyway so the module never risks a
# live connection if that changes.
os.environ["COPILOT_FAKE_SNOWFLAKE"] = "1"

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


def test_describe_table_returns_card():
    card = mcp_srv._describe_table_impl("GOLD.DIM_MACHINE", CardFake())
    assert "machine" in card.lower()
