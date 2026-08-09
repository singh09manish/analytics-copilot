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


@patch("copilot.snowflake_client.get_settings")
@patch("copilot.snowflake_client.snowflake.connector.connect")
@patch("copilot.snowflake_client._load_private_key", return_value=b"DERKEY")
def test_connection_disables_secondary_roles(_key, connect, settings):
    """Verify that _connection() executes USE SECONDARY ROLES NONE to enforce role isolation."""
    settings.return_value = _fake_settings()
    init_cur = MagicMock()
    query_cur = MagicMock()
    query_cur.description = []
    query_cur.fetchall.return_value = []
    # First cursor call is for "USE SECONDARY ROLES NONE", second is for run_query
    connect.return_value.cursor.side_effect = [init_cur, query_cur]
    client = SnowflakeClient(role="COPILOT_APP_RO")
    client.run_query("SELECT 1")
    # Verify the secondary roles command was executed
    init_cur.execute.assert_called_once_with("USE SECONDARY ROLES NONE")
    init_cur.close.assert_called_once()


@patch("copilot.snowflake_client.get_settings")
@patch("copilot.snowflake_client.snowflake.connector.connect")
@patch("copilot.snowflake_client._load_private_key", return_value=b"DERKEY")
def test_reconnect_path_re_pins(_key, connect, settings):
    """Verify that reconnect path (is_closed() True) re-pins the secondary roles."""
    settings.return_value = _fake_settings()
    mock_conn_1 = MagicMock()
    mock_conn_2 = MagicMock()
    init_cur_1 = MagicMock()
    init_cur_2 = MagicMock()
    query_cur = MagicMock()
    query_cur.description = []
    query_cur.fetchall.return_value = []
    # First connect returns mock_conn_1, second connect returns mock_conn_2
    connect.side_effect = [mock_conn_1, mock_conn_2]
    # Cursors for first init, second init, and query
    mock_conn_1.cursor.side_effect = [init_cur_1]
    mock_conn_2.cursor.side_effect = [init_cur_2, query_cur]
    # Simulate first connection closed on second call
    mock_conn_1.is_closed.return_value = True
    mock_conn_2.is_closed.return_value = False
    client = SnowflakeClient(role="COPILOT_APP_RO")
    # First connection
    client._connection()
    init_cur_1.execute.assert_called_once_with("USE SECONDARY ROLES NONE")
    # Second connection (reconnect)
    client._connection()
    init_cur_2.execute.assert_called_once_with("USE SECONDARY ROLES NONE")
    # Verify both connections were pinned
    assert connect.call_count == 2


@patch("copilot.snowflake_client.get_settings")
@patch("copilot.snowflake_client.snowflake.connector.connect")
@patch("copilot.snowflake_client._load_private_key", return_value=b"DERKEY")
def test_fail_open_guard_closes_and_re_pins(_key, connect, settings):
    """Verify fail-open guard: pin failure closes conn and allows fresh reconnect."""
    settings.return_value = _fake_settings()
    mock_conn_1 = MagicMock()
    mock_conn_2 = MagicMock()
    init_cur_fail = MagicMock()
    init_cur_2 = MagicMock()
    query_cur = MagicMock()
    query_cur.description = []
    query_cur.fetchall.return_value = []
    # First connect fails on pin, second connect succeeds
    connect.side_effect = [mock_conn_1, mock_conn_2]
    # First pin fails, second succeeds
    init_cur_fail.execute.side_effect = Exception("pin failed")
    mock_conn_1.cursor.return_value = init_cur_fail
    mock_conn_2.cursor.side_effect = [init_cur_2, query_cur]
    mock_conn_2.is_closed.return_value = False
    client = SnowflakeClient(role="COPILOT_APP_RO")
    # First connection should fail and close the connection
    try:
        client._connection()
        assert False, "Expected exception from pin failure"
    except Exception as e:  # noqa: BLE001
        assert str(e) == "pin failed"
    # Verify first connection was closed
    mock_conn_1.close.assert_called_once()
    # Second connection should succeed
    client.run_query("SELECT 1")
    # Verify second connection was created and pinned
    assert connect.call_count == 2
    init_cur_2.execute.assert_called_once_with("USE SECONDARY ROLES NONE")
