from unittest.mock import MagicMock, patch

import pytest
from snowflake.connector import errors as sf_errors

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


def _expired_token_error() -> sf_errors.ProgrammingError:
    """The exact error seen in production CloudWatch (/ecs/analytics-copilot)."""
    return sf_errors.ProgrammingError(
        msg="Authentication token has expired.  The user must authenticate again.",
        errno=390114, sqlstate="08001")


def _sql_compilation_error() -> sf_errors.ProgrammingError:
    """Same exception class, deterministic cause: must never be retried."""
    return sf_errors.ProgrammingError(
        msg="SQL compilation error: invalid identifier 'MODELL'",
        errno=904, sqlstate="42000")


def _live_conn() -> MagicMock:
    """A connection an expired token leaves behind: is_closed() stays False."""
    conn = MagicMock()
    conn.is_closed.return_value = False
    return conn


def _result_cursor(description=None, rows=()) -> MagicMock:
    cur = MagicMock()
    cur.description = description or []
    cur.fetchall.return_value = list(rows)
    return cur


@patch("copilot.snowflake_client.get_settings")
@patch("copilot.snowflake_client.snowflake.connector.connect")
@patch("copilot.snowflake_client._load_private_key", return_value=b"DERKEY")
def test_connect_enables_session_keep_alive(_key, connect, settings):
    """Without keep-alive an idle session lapses after the master token's 4h validity."""
    settings.return_value = _fake_settings()
    connect.return_value.cursor.return_value = _result_cursor()
    SnowflakeClient(role="COPILOT_APP_RO").run_query("SELECT 1")
    assert connect.call_args.kwargs["client_session_keep_alive"] is True


@patch("copilot.snowflake_client.get_settings")
@patch("copilot.snowflake_client.snowflake.connector.connect")
@patch("copilot.snowflake_client._load_private_key", return_value=b"DERKEY")
def test_expired_token_reconnects_retries_once_and_re_pins(_key, connect, settings):
    """390114 on a still-open connection: reconnect, retry once, and -- critically --
    re-apply USE SECONDARY ROLES NONE on the replacement session."""
    settings.return_value = _fake_settings()
    conn_dead, conn_fresh = _live_conn(), _live_conn()
    pin_dead, pin_fresh = MagicMock(), MagicMock()
    expired_cur = MagicMock()
    expired_cur.execute.side_effect = _expired_token_error()
    good_cur = _result_cursor([("N",), ("V",)], [(1, "a")])
    connect.side_effect = [conn_dead, conn_fresh]
    conn_dead.cursor.side_effect = [pin_dead, expired_cur]
    conn_fresh.cursor.side_effect = [pin_fresh, good_cur]

    client = SnowflakeClient(role="COPILOT_APP_RO")
    cols, rows = client.run_query("SELECT 1")

    assert cols == ["N", "V"] and rows == [(1, "a")]  # the retry's result, not an error
    assert connect.call_count == 2
    pin_dead.execute.assert_called_once_with("USE SECONDARY ROLES NONE")
    # The security invariant: a reconnect that skipped this would silently restore
    # secondary-role privilege stacking.
    pin_fresh.execute.assert_called_once_with("USE SECONDARY ROLES NONE")
    assert connect.call_args.kwargs["role"] == "COPILOT_APP_RO"
    conn_dead.close.assert_called_once()  # the dead session is not leaked
    expired_cur.close.assert_called_once()


@patch("copilot.snowflake_client.get_settings")
@patch("copilot.snowflake_client.snowflake.connector.connect")
@patch("copilot.snowflake_client._load_private_key", return_value=b"DERKEY")
def test_sql_compilation_error_is_not_retried(_key, connect, settings):
    """Same exception class as the expired token, but deterministic -- retrying it
    would double-execute a statement whose failure came *after* work could happen."""
    settings.return_value = _fake_settings()
    conn = _live_conn()
    pin_cur, bad_cur = MagicMock(), MagicMock()
    bad_cur.execute.side_effect = _sql_compilation_error()
    connect.return_value = conn
    conn.cursor.side_effect = [pin_cur, bad_cur]

    client = SnowflakeClient(role="COPILOT_APP_RO")
    with pytest.raises(sf_errors.ProgrammingError) as excinfo:
        client.run_query("SELECT modell FROM GOLD.DIM_MACHINE")

    assert excinfo.value.errno == 904
    assert connect.call_count == 1  # never reconnected
    conn.close.assert_not_called()  # the healthy session is kept


@patch("copilot.snowflake_client.get_settings")
@patch("copilot.snowflake_client.snowflake.connector.connect")
@patch("copilot.snowflake_client._load_private_key", return_value=b"DERKEY")
def test_expired_token_is_retried_at_most_once(_key, connect, settings):
    """A second expired token propagates instead of looping."""
    settings.return_value = _fake_settings()
    conn_a, conn_b = _live_conn(), _live_conn()
    pin_a, pin_b = MagicMock(), MagicMock()
    dead_a, dead_b = MagicMock(), MagicMock()
    dead_a.execute.side_effect = _expired_token_error()
    dead_b.execute.side_effect = _expired_token_error()
    connect.side_effect = [conn_a, conn_b]
    conn_a.cursor.side_effect = [pin_a, dead_a]
    conn_b.cursor.side_effect = [pin_b, dead_b]

    client = SnowflakeClient(role="COPILOT_APP_RO")
    with pytest.raises(sf_errors.ProgrammingError) as excinfo:
        client.run_query("SELECT 1")

    assert excinfo.value.errno == 390114
    assert connect.call_count == 2  # exactly one reconnect, no third attempt


@patch("copilot.snowflake_client.get_settings")
@patch("copilot.snowflake_client.snowflake.connector.connect")
@patch("copilot.snowflake_client._load_private_key", return_value=b"DERKEY")
def test_session_gone_text_is_matched_without_a_known_errno(_key, connect, settings):
    """Fallback path: an unrecognised errno still reconnects if the message says the
    session is gone, so a connector renumbering does not resurrect the outage."""
    settings.return_value = _fake_settings()
    conn_dead, conn_fresh = _live_conn(), _live_conn()
    pin_dead, pin_fresh = MagicMock(), MagicMock()
    gone_cur = MagicMock()
    gone_cur.execute.side_effect = sf_errors.DatabaseError(
        msg="Session no longer exists. New login required to access the service.",
        errno=999999)
    connect.side_effect = [conn_dead, conn_fresh]
    conn_dead.cursor.side_effect = [pin_dead, gone_cur]
    conn_fresh.cursor.side_effect = [pin_fresh, _result_cursor([("N",)], [(1,)])]

    cols, rows = SnowflakeClient(role="COPILOT_APP_RO").run_query("SELECT 1")

    assert (cols, rows) == (["N"], [(1,)])
    pin_fresh.execute.assert_called_once_with("USE SECONDARY ROLES NONE")


@patch("copilot.snowflake_client.get_settings")
@patch("copilot.snowflake_client.snowflake.connector.connect")
@patch("copilot.snowflake_client._load_private_key", return_value=b"DERKEY")
def test_racing_reconnect_reuses_the_winners_session(_key, connect, settings):
    """Two threadpool threads hitting the same expiry must not both rebuild. Driven
    deterministically: the second _reconnect() presents the same stale handle."""
    settings.return_value = _fake_settings()
    conn_stale, conn_fresh = _live_conn(), _live_conn()
    connect.side_effect = [conn_stale, conn_fresh]
    conn_stale.cursor.side_effect = [MagicMock()]
    conn_fresh.cursor.side_effect = [MagicMock()]

    client = SnowflakeClient(role="COPILOT_APP_RO")
    stale = client._connection()
    assert client._reconnect(stale) is conn_fresh  # winner rebuilds
    assert client._reconnect(stale) is conn_fresh  # loser reuses, does not rebuild
    assert connect.call_count == 2


@patch("copilot.snowflake_client.get_settings")
@patch("copilot.snowflake_client.snowflake.connector.connect")
@patch("copilot.snowflake_client._load_private_key", return_value=b"DERKEY")
def test_execute_many_inherits_the_retry(_key, connect, settings):
    """Writes go through execute_many/run_query. Retrying is safe because Snowflake
    rejects the expired token before the statement runs -- no double INSERT."""
    settings.return_value = _fake_settings()
    conn_dead, conn_fresh = _live_conn(), _live_conn()
    pin_dead, pin_fresh = MagicMock(), MagicMock()
    expired_cur = MagicMock()
    expired_cur.execute.side_effect = _expired_token_error()
    connect.side_effect = [conn_dead, conn_fresh]
    conn_dead.cursor.side_effect = [pin_dead, expired_cur]
    conn_fresh.cursor.side_effect = [pin_fresh, _result_cursor(), _result_cursor()]

    SnowflakeClient(role="COPILOT_APP_WRITER").execute_many(
        ["INSERT INTO OPS.REQUEST_LOG VALUES (1)", "INSERT INTO OPS.REQUEST_LOG VALUES (2)"])

    assert connect.call_count == 2
    pin_fresh.execute.assert_called_once_with("USE SECONDARY ROLES NONE")
