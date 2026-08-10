import threading
from pathlib import Path

import snowflake.connector
from cryptography.hazmat.primitives import serialization

from copilot.config import REPO_ROOT, get_settings


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

    def _connection(self) -> snowflake.connector.SnowflakeConnection:
        if self._conn is not None and not self._conn.is_closed():
            return self._conn
        with self._conn_lock:
            if self._conn is not None and not self._conn.is_closed():
                return self._conn  # lost the race; another thread connected
            s = self._settings
            conn = snowflake.connector.connect(
                account=s.snowflake_account,
                user=s.snowflake_user,
                private_key=_load_private_key(
                    s.snowflake_private_key_path, s.snowflake_private_key_pem),
                warehouse=s.snowflake_warehouse,
                database=s.snowflake_database,
                role=self._role,
                client_session_keep_alive=False,
            )
            # Disable secondary roles to enforce primary role isolation.
            # By default, Snowflake users have DEFAULT_SECONDARY_ROLES = ('ALL'),
            # which allows a session to exercise privileges of all roles the user holds.
            # Pinning to primary role only ensures RBAC is enforced strictly.
            # Connect into local conn, pin, then assign to cache.
            # If pin fails, close the connection and leave cache empty (fail-safe).
            try:
                cur = conn.cursor()
                try:
                    cur.execute("USE SECONDARY ROLES NONE")
                finally:
                    cur.close()
            except Exception:
                conn.close()
                raise
            self._conn = conn
        return self._conn

    def run_query(self, sql: str, params: tuple = ()) -> tuple[list[str], list[tuple]]:
        cur = self._connection().cursor()
        try:
            cur.execute(sql, params or None)
            cols = [d[0] for d in cur.description] if cur.description else []
            return cols, cur.fetchall()
        finally:
            cur.close()

    def execute_many(self, statements: list[str]) -> None:
        for stmt in statements:
            self.run_query(stmt)
