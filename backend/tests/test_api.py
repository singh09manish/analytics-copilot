from fastapi.testclient import TestClient

from copilot.api.main import app
from tests.conftest import FakeProvider, FakeSnowflake


def _client():
    app.state.provider = FakeProvider()
    app.state.sf_ro = FakeSnowflake()
    return TestClient(app)


def test_healthz():
    assert _client().get("/healthz").json() == {"status": "ok"}


def test_chat_happy_path():
    r = _client().post("/chat", json={"question": "Which models?"})
    assert r.status_code == 200
    body = r.json()
    assert body["answer"] == "Here you go."
    assert "LIMIT 1000" in body["sql"]


def test_chat_empty_question_400():
    assert _client().post("/chat", json={"question": "  "}).status_code == 400
