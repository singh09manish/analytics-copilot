from pathlib import Path

import snowflake.connector
from cryptography.hazmat.primitives import serialization

from copilot.config import REPO_ROOT, get_settings


def _load_private_key(path: str) -> bytes:
    p = Path(path)
    if not p.is_absolute():
        p = REPO_ROOT / p
    key = serialization.load_pem_private_key(p.read_bytes(), password=None)
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

    def _connection(self) -> snowflake.connector.SnowflakeConnection:
        if self._conn is None or self._conn.is_closed():
            s = self._settings
            self._conn = snowflake.connector.connect(
                account=s.snowflake_account,
                user=s.snowflake_user,
                private_key=_load_private_key(s.snowflake_private_key_path),
                warehouse=s.snowflake_warehouse,
                database=s.snowflake_database,
                role=self._role,
                client_session_keep_alive=False,
            )
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
