import bcrypt
from fastapi.testclient import TestClient

from copilot import auth
from copilot.api.main import app
from tests.conftest import FakeProvider, FakeSnowflake


def _client(monkeypatch):
    h = bcrypt.hashpw(b"pw123", bcrypt.gensalt()).decode()
    s = auth.get_settings()
    monkeypatch.setattr(s, "demo_analyst_password_hash", h)
    monkeypatch.setattr(s, "demo_admin_password_hash", h)
    monkeypatch.setattr(s, "jwt_secret", "test-secret")
    app.state.provider = FakeProvider()
    app.state.sf_ro = FakeSnowflake()
    app.state.sf_admin = FakeSnowflake()
    app.state.sf_writer = FakeSnowflake()
    app.state.executor = None
    return TestClient(app)


def _token(client, email="analyst@demo"):
    r = client.post("/auth/login", json={"email": email, "password": "pw123"})
    assert r.status_code == 200
    return r.json()


def test_login_success_and_failure(monkeypatch):
    c = _client(monkeypatch)
    body = _token(c, "admin@demo")
    assert body["role"] == "admin" and body["token"]
    assert c.post("/auth/login", json={"email": "admin@demo", "password": "no"}).status_code == 401


def test_chat_requires_auth(monkeypatch):
    c = _client(monkeypatch)
    assert c.post("/chat", json={"question": "hi"}).status_code == 401


def test_chat_uses_role_scoped_session_and_logs(monkeypatch):
    c = _client(monkeypatch)
    tok = _token(c)["token"]
    r = c.post("/chat", json={"question": "Which models?"},
               headers={"authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert r.json()["request_id"]
    assert app.state.sf_ro.queries  # analyst hit the RO session
    assert not app.state.sf_admin.queries
    assert any("REQUEST_LOG" in q for q in app.state.sf_writer.queries)


def test_admin_chat_uses_admin_session(monkeypatch):
    c = _client(monkeypatch)
    tok = _token(c, "admin@demo")["token"]
    c.post("/chat", json={"question": "Which models?"},
           headers={"authorization": f"Bearer {tok}"})
    assert app.state.sf_admin.queries


def test_feedback_recorded(monkeypatch):
    c = _client(monkeypatch)
    tok = _token(c)["token"]
    r = c.post("/feedback",
               json={"request_id": "rid", "conversation_id": "c1",
                     "rating": "down", "comment": "wrong number"},
               headers={"authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert any("INSERT INTO MEDTECH_ANALYTICS.COPILOT.FEEDBACK" in q
               for q in app.state.sf_writer.queries)


def test_healthz_open():
    assert TestClient(app).get("/healthz").json() == {"status": "ok"}


class _TrackingProvider(FakeProvider):
    """FakeProvider that records every structured() prompt it was given, so tests
    can inspect whether conversation history leaked into a later call's prompt."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.structured_calls = []

    def structured(self, system, user, schema, max_tokens=1500):
        self.structured_calls.append((schema.__name__, user))
        return super().structured(system, user, schema, max_tokens)


def test_same_conversation_id_does_not_leak_across_users(monkeypatch):
    """Security regression (Task 4 review): the LangGraph checkpointer is a
    process-global InMemorySaver keyed by thread_id. If the API passed the
    client-supplied conversation_id straight through, two different users
    reusing the same conversation_id (e.g. both starting from "1") would share
    each other's conversation history. The API must namespace the thread key
    by authenticated identity so this can't happen."""
    c = _client(monkeypatch)
    tracking = _TrackingProvider()
    app.state.provider = tracking

    tok_analyst = _token(c, "analyst@demo")["token"]
    tok_admin = _token(c, "admin@demo")["token"]
    shared_conversation_id = "shared-conv"

    r1 = c.post("/chat", json={"question": "Which models?",
                                "conversation_id": shared_conversation_id},
               headers={"authorization": f"Bearer {tok_analyst}"})
    assert r1.status_code == 200

    r2 = c.post("/chat", json={"question": "unrelated admin question",
                                "conversation_id": shared_conversation_id},
               headers={"authorization": f"Bearer {tok_admin}"})
    assert r2.status_code == 200

    sql_user_inputs = [u for n, u in tracking.structured_calls if n == "SqlDraft"]
    # If the analyst's turn leaked into the admin's session, the admin's SqlDraft
    # prompt would contain the analyst's earlier question as "recent conversation".
    assert "Which models?" not in sql_user_inputs[-1]
