from copilot.api.main import app
from tests.test_api import _client, _token

# Reuse the real helpers from test_api.py rather than inventing a `client` fixture:
# `_client(monkeypatch)` builds a hermetic TestClient with FakeProvider/FakeSnowflake
# on app.state, and `_token(client, email)` logs in and returns the auth payload.


def _auth(client, role: str) -> dict:
    tok = _token(client, f"{role}@demo")["token"]
    return {"authorization": f"Bearer {tok}"}


def test_admin_endpoints_reject_analyst_with_403(monkeypatch):
    c = _client(monkeypatch)
    headers = _auth(c, "analyst")
    for path in ("/api/admin/overview", "/api/admin/requests", "/api/admin/feedback"):
        r = c.get(path, headers=headers)
        assert r.status_code == 403, path


def test_admin_endpoints_reject_anonymous_with_401(monkeypatch):
    c = _client(monkeypatch)
    for path in ("/api/admin/overview", "/api/admin/requests", "/api/admin/feedback"):
        assert c.get(path).status_code == 401, path


def test_admin_overview_shape(monkeypatch):
    c = _client(monkeypatch)
    r = c.get("/api/admin/overview", headers=_auth(c, "admin"))
    assert r.status_code == 200
    body = r.json()
    for key in ("total_requests", "error_rate", "p50_latency_ms", "feedback_up",
                "feedback_down", "by_intent"):
        assert key in body, key


def test_admin_reads_use_the_admin_session(monkeypatch):
    """Ops tables are readable only by COPILOT_ADMIN -- if these ever ran on the
    analyst session they would fail live while passing hermetically."""
    c = _client(monkeypatch)
    c.get("/api/admin/requests", headers=_auth(c, "admin"))
    assert app.state.sf_admin.queries, "admin endpoint did not query the admin session"
    assert not app.state.sf_ro.queries, "admin endpoint must not touch the analyst session"


def test_admin_requests_shape(monkeypatch):
    c = _client(monkeypatch)
    r = c.get("/api/admin/requests", headers=_auth(c, "admin"))
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    for key in ("request_id", "created_at", "user_role", "intent", "status",
                "error_type", "e2e_ms", "sql_text", "question"):
        assert key in body[0], key


def test_admin_feedback_shape(monkeypatch):
    c = _client(monkeypatch)
    r = c.get("/api/admin/feedback", headers=_auth(c, "admin"))
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    for key in ("feedback_id", "created_at", "conversation_id", "request_id",
                "rating", "comment"):
        assert key in body[0], key


def test_admin_requests_limit_is_bound_and_clamped(monkeypatch):
    """limit must never be interpolated into SQL, and must be clamped to 1..500."""
    c = _client(monkeypatch)
    headers = _auth(c, "admin")

    c.get("/api/admin/requests?limit=9999", headers=headers)
    sql, params = app.state.sf_admin.calls[-1]
    assert "9999" not in sql
    assert params == (500,)

    c.get("/api/admin/requests?limit=0", headers=headers)
    sql, params = app.state.sf_admin.calls[-1]
    assert params == (1,)

    c.get("/api/admin/requests?limit=25", headers=headers)
    sql, params = app.state.sf_admin.calls[-1]
    assert params == (25,)


def test_admin_feedback_limit_is_bound_and_clamped(monkeypatch):
    c = _client(monkeypatch)
    headers = _auth(c, "admin")
    c.get("/api/admin/feedback?limit=-5", headers=headers)
    sql, params = app.state.sf_admin.calls[-1]
    assert "-5" not in sql
    assert params == (1,)


def test_admin_endpoints_return_503_on_snowflake_failure(monkeypatch):
    c = _client(monkeypatch)
    headers = _auth(c, "admin")
    app.state.sf_admin.fail = True
    for path in ("/api/admin/overview", "/api/admin/requests", "/api/admin/feedback"):
        r = c.get(path, headers=headers)
        assert r.status_code == 503, path
