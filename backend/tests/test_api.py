import logging
import threading
import time
from datetime import UTC, datetime, timedelta

import bcrypt
import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

from copilot import auth
from copilot.api import main as main_module
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


# --- Task 7 follow-up review, Finding 1: query EXECUTION (not just retrieval)
# must run under the caller's verified role, or an admin's masked-view query
# executed through MCP under the RO role would come back masked.


class _RoleTrackingExecutor:
    """Fake state.executor: records the Snowflake role each run_query call
    received. Mirrors McpExecutor's (sql, role="COPILOT_APP_RO") -> (cols, rows)
    shape so main.py's functools.partial(state.executor.run_query, role=...)
    wiring is exercised exactly as in production."""

    def __init__(self):
        self.calls = []

    def run_query(self, sql, role="COPILOT_APP_RO"):
        self.calls.append((sql, role))
        return (["MODEL"], [("TrueBeam",)])


def test_admin_chat_routes_mcp_execution_to_copilot_admin_role(monkeypatch):
    c = _client(monkeypatch)
    executor = _RoleTrackingExecutor()
    app.state.executor = executor
    tok = _token(c, "admin@demo")["token"]
    r = c.post("/chat", json={"question": "Which models?"},
               headers={"authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert executor.calls
    assert all(role == "COPILOT_ADMIN" for _sql, role in executor.calls)


def test_analyst_chat_routes_mcp_execution_to_copilot_app_ro_role(monkeypatch):
    c = _client(monkeypatch)
    executor = _RoleTrackingExecutor()
    app.state.executor = executor
    tok = _token(c)["token"]
    r = c.post("/chat", json={"question": "Which models?"},
               headers={"authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert executor.calls
    assert all(role == "COPILOT_APP_RO" for _sql, role in executor.calls)


# --- Task 7 follow-up review, Finding 2: concurrent first requests during a slow
# cold start (production: 3 Snowflake connects + an MCP subprocess spawn, up to
# 60s) must never observe a half-initialized app.state.


def _reset_deps_state():
    for attr in ("provider", "sf_ro", "sf_admin", "sf_writer", "executor"):
        if hasattr(app.state, attr):
            delattr(app.state, attr)


def test_deps_cold_start_is_race_free(monkeypatch):
    _reset_deps_state()
    s = auth.get_settings()
    monkeypatch.setattr(s, "use_mcp", True)

    build_calls = {"provider": 0, "sf": 0, "executor": 0}
    counter_lock = threading.Lock()

    class SlowFakeProvider:
        def __init__(self):
            with counter_lock:
                build_calls["provider"] += 1
            time.sleep(0.05)

    class SlowFakeSnowflakeClient:
        def __init__(self, role=None):
            with counter_lock:
                build_calls["sf"] += 1
            time.sleep(0.05)
            self.role = role

    class SlowFakeExecutor:
        def __init__(self):
            with counter_lock:
                build_calls["executor"] += 1
            time.sleep(0.05)

        def run_query(self, sql, role="COPILOT_APP_RO"):
            return (["A"], [[1]])

        def close(self):
            pass

    monkeypatch.setattr("copilot.llm.provider.AnthropicProvider", SlowFakeProvider)
    monkeypatch.setattr("copilot.snowflake_client.SnowflakeClient", SlowFakeSnowflakeClient)
    monkeypatch.setattr("copilot.mcp_client.McpExecutor", SlowFakeExecutor)

    results = []
    errors = []
    results_lock = threading.Lock()

    def worker():
        try:
            state = main_module._deps()
            # every attribute must show up together -- never a torn read
            assert all(hasattr(state, a) for a in
                       ("provider", "sf_ro", "sf_admin", "sf_writer", "executor"))
            with results_lock:
                results.append(state)
        except Exception as e:  # noqa: BLE001 -- captured for the assertion below
            with results_lock:
                errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent cold start raised: {errors!r}"
    assert len(results) == 8
    # Each dependency was built exactly once, proving the lock serialized
    # construction instead of racing 8 separate builds.
    assert build_calls == {"provider": 1, "sf": 3, "executor": 1}
    _reset_deps_state()


# --- Task 7 follow-up review, Finding 3: an McpExecutor() construction failure
# must not 500 the first request, and the fallback must be a logged, deliberate
# decision instead of a silently-permanent one.


def test_deps_mcp_construction_failure_falls_back_gracefully(monkeypatch, caplog):
    _reset_deps_state()
    s = auth.get_settings()
    monkeypatch.setattr(s, "use_mcp", True)

    class DummyProvider:
        pass

    class DummySnowflakeClient:
        def __init__(self, role=None):
            self.role = role

    class ExplodingExecutor:
        def __init__(self):
            raise RuntimeError("mcp subprocess failed to start")

    monkeypatch.setattr("copilot.llm.provider.AnthropicProvider", DummyProvider)
    monkeypatch.setattr("copilot.snowflake_client.SnowflakeClient", DummySnowflakeClient)
    monkeypatch.setattr("copilot.mcp_client.McpExecutor", ExplodingExecutor)

    with caplog.at_level(logging.WARNING):
        state = main_module._deps()  # must not raise

    assert state.executor is None
    assert state.provider is not None and state.sf_ro is not None  # rest still published
    assert "mcp" in caplog.text.lower()  # the fallback is observable, not silent
    _reset_deps_state()


# --- Final review, Critical finding C2: an McpExecutor whose subprocess has died
# must not stay on app.state for the process lifetime. /chat replaces it, and if the
# replacement can't be built it degrades to direct execution rather than 500-ing or
# blocking (safe now that the side-effect denylist lives in sql_guard, layer 1).


class _DeadExecutor:
    def __init__(self):
        self.closed = False

    def is_broken(self):
        return True

    def close(self):
        self.closed = True

    def run_query(self, sql, role="COPILOT_APP_RO"):
        raise AssertionError("a dead executor must never be asked to run a query")


def test_chat_replaces_a_dead_mcp_executor(monkeypatch):
    c = _client(monkeypatch)
    dead = _DeadExecutor()
    app.state.executor = dead
    fresh = _RoleTrackingExecutor()
    monkeypatch.setattr(main_module, "_build_executor", lambda ready_timeout=None: fresh)

    tok = _token(c)["token"]
    r = c.post("/chat", json={"question": "Which models?"},
               headers={"authorization": f"Bearer {tok}"})

    assert r.status_code == 200
    assert dead.closed, "the corpse must be torn down, not just dropped"
    assert app.state.executor is fresh
    assert fresh.calls, "the rebuilt executor should serve the request"
    app.state.executor = None


def test_chat_falls_back_to_direct_execution_when_the_rebuild_fails(monkeypatch, caplog):
    c = _client(monkeypatch)
    app.state.executor = _DeadExecutor()
    monkeypatch.setattr(main_module, "_build_executor", lambda ready_timeout=None: None)

    tok = _token(c)["token"]
    with caplog.at_level(logging.WARNING):
        r = c.post("/chat", json={"question": "Which models?"},
                   headers={"authorization": f"Bearer {tok}"})

    assert r.status_code == 200
    assert app.state.executor is None
    assert app.state.sf_ro.queries, "should have fallen back to the role-scoped session"
    assert "mcp" in caplog.text.lower(), "the degradation must be observable"


def test_chat_keeps_a_healthy_mcp_executor(monkeypatch):
    """The liveness check must not churn a perfectly good executor."""
    c = _client(monkeypatch)

    class _HealthyExecutor(_RoleTrackingExecutor):
        def is_broken(self):
            return False

    healthy = _HealthyExecutor()
    app.state.executor = healthy
    monkeypatch.setattr(main_module, "_build_executor",
                        lambda ready_timeout=None: pytest.fail("must not rebuild"))

    tok = _token(c)["token"]
    r = c.post("/chat", json={"question": "Which models?"},
               headers={"authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert app.state.executor is healthy
    app.state.executor = None


# --- Task 7 follow-up review, Finding 4: the app must refuse to start with the
# published default JWT secret still in force (it's the whole authorization
# boundary for role-scoped Snowflake access). Lifespan only runs when TestClient
# is used as a context manager, so this test opts into that explicitly; every
# other test in this file uses TestClient(app) without `with`, which never
# triggers lifespan and stays hermetic regardless of this check.


def test_app_refuses_to_start_with_default_jwt_secret(monkeypatch):
    s = auth.get_settings()
    monkeypatch.setattr(s, "jwt_secret", "dev-secret-change-me")
    with pytest.raises(RuntimeError, match="JWT_SECRET"), TestClient(app):
        pass


# --- Task 7 follow-up review, Finding 5: a validly-signed token missing an
# expected claim must map to 401, not an uncaught KeyError -> 500.


def test_chat_token_without_sub_claim_is_401_not_500(monkeypatch):
    c = _client(monkeypatch)
    s = auth.get_settings()
    bad = pyjwt.encode({"role": "analyst"}, s.jwt_secret, algorithm="HS256")  # no "sub"
    r = c.post("/chat", json={"question": "hi"},
               headers={"authorization": f"Bearer {bad}"})
    assert r.status_code == 401


# --- Task 7 follow-up review, Finding 6: the token subject must be the SAME
# normalized email the login response echoes, or "analyst@demo" and
# "  Analyst@Demo  " mint two different checkpointer identities for one account.


def test_login_normalizes_email_for_token_subject(monkeypatch):
    c = _client(monkeypatch)
    r = c.post("/auth/login", json={"email": "  Analyst@Demo  ", "password": "pw123"})
    assert r.status_code == 200
    body = r.json()
    assert body["email"] == "analyst@demo"
    payload = auth.decode_token(body["token"])
    assert payload["sub"] == "analyst@demo"


# --- Task 7 follow-up review, Finding 7: ':' is the checkpointer thread-key
# separator; a client-supplied conversation_id containing one must be rejected
# so the composed key can never be ambiguous.


def test_chat_rejects_conversation_id_containing_colon(monkeypatch):
    c = _client(monkeypatch)
    tok = _token(c)["token"]
    r = c.post("/chat", json={"question": "hi", "conversation_id": "evil:conv"},
               headers={"authorization": f"Bearer {tok}"})
    assert r.status_code == 400


# --- Task 7 follow-up review, Finding 8: the MCP subprocess/background loop must
# be closed on app shutdown, not orphaned. Like Finding 4's test, this needs
# `with TestClient(app)` to actually run the lifespan's shutdown half.


def test_app_shutdown_closes_mcp_executor(monkeypatch):
    s = auth.get_settings()
    monkeypatch.setattr(s, "jwt_secret", "test-secret")

    closed = {"called": False}

    class FakeExecutorWithClose:
        def close(self):
            closed["called"] = True

    app.state.provider = FakeProvider()
    app.state.sf_ro = FakeSnowflake()
    app.state.sf_admin = FakeSnowflake()
    app.state.sf_writer = FakeSnowflake()
    app.state.executor = FakeExecutorWithClose()

    with TestClient(app):
        pass

    assert closed["called"] is True
    _reset_deps_state()


# --- Task 7 follow-up review, Finding 9: additional coverage requested by the
# review -- auth-required paths return 401 (not 500) on bad/missing/expired
# tokens, a REQUEST_LOG writer outage still returns 200, and the empty-question
# 400 path (dropped when test_api.py was rewritten for auth) is restored.


def test_feedback_requires_auth(monkeypatch):
    c = _client(monkeypatch)
    r = c.post("/feedback", json={"request_id": "r", "conversation_id": None,
                                   "rating": "up", "comment": None})
    assert r.status_code == 401


def test_chat_garbage_token_is_401_not_500(monkeypatch):
    c = _client(monkeypatch)
    r = c.post("/chat", json={"question": "hi"},
               headers={"authorization": "Bearer not-a-real-token"})
    assert r.status_code == 401


def test_chat_expired_token_is_401_not_500(monkeypatch):
    c = _client(monkeypatch)
    s = auth.get_settings()
    expired = pyjwt.encode(
        {"sub": "analyst@demo", "role": "analyst", "exp": datetime.now(UTC) - timedelta(hours=1)},
        s.jwt_secret, algorithm="HS256")
    r = c.post("/chat", json={"question": "hi"},
               headers={"authorization": f"Bearer {expired}"})
    assert r.status_code == 401


def test_chat_returns_200_even_when_request_log_writer_is_down(monkeypatch):
    class DeadWriter:
        def run_query(self, sql, params=()):
            raise RuntimeError("writer down")

    c = _client(monkeypatch)
    app.state.sf_writer = DeadWriter()
    tok = _token(c)["token"]
    r = c.post("/chat", json={"question": "Which models?"},
               headers={"authorization": f"Bearer {tok}"})
    assert r.status_code == 200


def test_chat_empty_question_400(monkeypatch):
    c = _client(monkeypatch)
    tok = _token(c)["token"]
    r = c.post("/chat", json={"question": "  "},
               headers={"authorization": f"Bearer {tok}"})
    assert r.status_code == 400
