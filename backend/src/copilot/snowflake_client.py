import logging
import threading
from pathlib import Path

import snowflake.connector
from cryptography.hazmat.primitives import serialization
from snowflake.connector import errors as sf_errors

from copilot.config import REPO_ROOT, get_settings

logger = logging.getLogger(__name__)

# Snowflake GS codes that mean "this session is no longer authenticated", as opposed
# to "your SQL is wrong". Names mirror the connector's own constants in
# snowflake.connector.network. 390114 (MASTER_TOKEN_EXPIRED) is the one seen in
# production: "390114 (08001): Authentication token has expired. The user must
# authenticate again." Its siblings cover an id token that lapsed, a session the
# server no longer knows about, and a master token that was dropped or invalidated.
#
# This set is deliberately narrow. run_query() retries exactly these; anything else
# -- above all a SQL compilation error, which is also a ProgrammingError -- must
# propagate on the first failure.
_SESSION_GONE_ERRNOS = frozenset({
    390110,  # ID_TOKEN_EXPIRED
    390111,  # session no longer exists; new login required
    390112,  # SESSION_EXPIRED
    390113,  # MASTER_TOKEN_NOTFOUND
    390114,  # MASTER_TOKEN_EXPIRED  <- the production traceback
    390115,  # MASTER_TOKEN_INVALID
    390195,  # ID_TOKEN_INVALID_LOGIN_REQUEST
})

# Belt-and-braces text match, in case a future connector release renumbers or wraps
# the code. Matched case-insensitively against the error message.
_SESSION_GONE_TEXT = (
    "authentication token has expired",
    "session no longer exists",
    "session token expired",
    "session expired",
)


def _is_expired_session_error(exc: BaseException) -> bool:
    """True only for the "you must authenticate again" class of Snowflake error.

    Restricted to snowflake.connector errors on purpose: a broad match here would
    turn a deterministic failure (bad SQL, missing table, insufficient privilege)
    into a silent double execution.
    """
    if not isinstance(exc, sf_errors.Error):
        return False
    errno = getattr(exc, "errno", None)
    if errno is not None:
        try:
            if int(errno) in _SESSION_GONE_ERRNOS:
                return True
        except (TypeError, ValueError):
            pass  # non-numeric errno; fall through to the text match
    message = str(getattr(exc, "msg", "") or "") + " " + str(exc)
    message = message.lower()
    return any(needle in message for needle in _SESSION_GONE_TEXT)


def _load_private_key(path: str, pem: str = "") -> bytes:
    if pem.strip():
        data = pem.encode()
    else:
        p = Path(path)
        if not p.is_absolute():
            p = REPO_ROOT / p
        if not p.exists():
            raise FileNotFoundError(
                f"No Snowflake private key: {p} does not exist and "
                "SNOWFLAKE_PRIVATE_KEY_PEM is empty. Set one of them.")
        data = p.read_bytes()
    key = serialization.load_pem_private_key(data, password=None)
    return key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


class SnowflakeClient:
    """Thin connection wrapper. One instance per role; connects lazily, reconnects if dropped."""

    def __init__(self, role: str | None = None):
        self._settings = get_settings()
        self._role = role or self._settings.snowflake_role
        self._conn: snowflake.connector.SnowflakeConnection | None = None
        # These clients are process-global (app.state.sf_ro/sf_admin/sf_writer) and
        # every sync endpoint runs in FastAPI's threadpool, so first use is genuinely
        # concurrent. Check-then-act on `_conn` let two threads each open a Snowflake
        # session, one of which was then overwritten and leaked until its idle
        # timeout. Connecting under the lock makes first use happen exactly once.
        self._conn_lock = threading.Lock()

    def _open_pinned_connection(self) -> snowflake.connector.SnowflakeConnection:
        """Connect and apply the security pin. MUST be called with _conn_lock held.

        Every path that produces a connection -- first use, is_closed() reconnect, and
        the expired-token retry -- goes through here, so the `USE SECONDARY ROLES NONE`
        pin can never be skipped on a replacement session.
        """
        s = self._settings
        conn = snowflake.connector.connect(
            account=s.snowflake_account,
            user=s.snowflake_user,
            private_key=_load_private_key(
                s.snowflake_private_key_path, s.snowflake_private_key_pem),
            warehouse=s.snowflake_warehouse,
            database=s.snowflake_database,
            role=self._role,
            # The task runs for days and these clients are process-global, so an idle
            # session would otherwise let its master token lapse (4h by default) and
            # every later query would fail with 390114. Keep-alive heartbeats the
            # session so it does not lapse from idleness in the first place; the retry
            # in run_query() is the backstop for when it lapses anyway.
            client_session_keep_alive=True,
        )
        # Disable secondary roles to enforce primary role isolation.
        # By default, Snowflake users have DEFAULT_SECONDARY_ROLES = ('ALL'),
        # which allows a session to exercise privileges of all roles the user holds.
        # Pinning to primary role only ensures RBAC is enforced strictly.
        # If the pin fails, close the connection and raise (fail-safe): an unpinned
        # session is a privilege-escalation hole, so no connection is better than one.
        try:
            cur = conn.cursor()
            try:
                cur.execute("USE SECONDARY ROLES NONE")
            finally:
                cur.close()
        except Exception:
            conn.close()
            raise
        return conn

    def _connection(self) -> snowflake.connector.SnowflakeConnection:
        if self._conn is not None and not self._conn.is_closed():
            return self._conn
        with self._conn_lock:
            if self._conn is not None and not self._conn.is_closed():
                return self._conn  # lost the race; another thread connected
            self._conn = self._open_pinned_connection()
        return self._conn

    def _reconnect(
        self, stale: snowflake.connector.SnowflakeConnection,
    ) -> snowflake.connector.SnowflakeConnection:
        """Replace `stale` with a freshly authenticated, freshly pinned session.

        An expired auth token does NOT close the connection -- is_closed() stays False
        -- so _connection() would hand the dead session back forever. This is the only
        way out of that state.

        Takes the same _conn_lock as _connection() so two threads that hit the expiry
        at the same moment cannot both rebuild: the loser sees that _conn has already
        moved on and reuses the winner's session.
        """
        with self._conn_lock:
            current = self._conn
            if current is not stale and current is not None and not current.is_closed():
                return current  # another thread already rebuilt after the same expiry
            if current is not None:
                try:
                    current.close()
                except Exception:  # the session is already gone server-side
                    logger.warning("Closing the expired Snowflake session failed; "
                                   "dropping the reference anyway.", exc_info=True)
            self._conn = None
            # Goes through _open_pinned_connection, so USE SECONDARY ROLES NONE is
            # re-applied. Skipping it here would silently restore secondary-role
            # privilege stacking on every token expiry -- a privilege escalation.
            self._conn = self._open_pinned_connection()
            return self._conn

    @staticmethod
    def _execute(conn: snowflake.connector.SnowflakeConnection, sql: str,
                 params: tuple) -> tuple[list[str], list[tuple]]:
        cur = conn.cursor()
        try:
            cur.execute(sql, params or None)
            cols = [d[0] for d in cur.description] if cur.description else []
            return cols, cur.fetchall()
        finally:
            cur.close()

    def run_query(self, sql: str, params: tuple = ()) -> tuple[list[str], list[tuple]]:
        conn = self._connection()
        try:
            return self._execute(conn, sql, params)
        except Exception as e:  # re-raised below unless it is a dead-session error
            if not _is_expired_session_error(e):
                raise
            # Safe for writes, not just reads: Snowflake rejects an expired auth token
            # before the statement is parsed, compiled or executed, so the failed
            # attempt cannot have applied an INSERT/MERGE that this retry would
            # duplicate. That guarantee is exactly why the retry is gated on the
            # expired-session error class and never on ProgrammingError in general --
            # a SQL error can fire after work has happened, so it is never retried.
            logger.warning(
                "Snowflake session was no longer authenticated (errno=%s); "
                "reconnecting and retrying this statement once.",
                getattr(e, "errno", None), exc_info=True)
        # Exactly once. This call is deliberately outside the try/except above, so a
        # second failure -- including a second expired token -- propagates to the caller.
        return self._execute(self._reconnect(conn), sql, params)

    def execute_many(self, statements: list[str]) -> None:
        # Delegates to run_query, so each statement inherits the reconnect-and-retry.
        for stmt in statements:
            self.run_query(stmt)
