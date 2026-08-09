from unittest.mock import MagicMock, patch

from copilot.snowflake_client import SnowflakeClient


def _fake_settings():
    s = MagicMock()
    s.snowflake_account = "acct"
    s.snowflake_user = "COPILOT_SVC"
    s.snowflake_private_key_path = "secrets/copilot_svc_key.p8"
    s.snowflake_warehouse = "COPILOT_WH"
    s.snowflake_database = "MEDTECH_ANALYTICS"
    s.snowflake_role = "COPILOT_APP_RO"
    return s


@patch("copilot.snowflake_client.get_settings")
@patch("copilot.snowflake_client.snowflake.connector.connect")
@patch("copilot.snowflake_client._load_private_key", return_value=b"DERKEY")
def test_run_query_returns_columns_and_rows(_key, connect, settings):
    settings.return_value = _fake_settings()
    cur = MagicMock()
    cur.description = [("N",), ("V",)]
    cur.fetchall.return_value = [(1, "a")]
    connect.return_value.cursor.return_value = cur
    client = SnowflakeClient(role="COPILOT_ADMIN")
    cols, rows = client.run_query("SELECT 1")
    assert cols == ["N", "V"] and rows == [(1, "a")]
    assert connect.call_args.kwargs["role"] == "COPILOT_ADMIN"
    assert connect.call_args.kwargs["private_key"] == b"DERKEY"
