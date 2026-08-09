from datetime import UTC, datetime, timedelta

import bcrypt
import jwt as pyjwt
import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from copilot import auth


@pytest.fixture(autouse=True)
def demo_hashes(monkeypatch):
    h = bcrypt.hashpw(b"pw123", bcrypt.gensalt()).decode()
    s = auth.get_settings()
    monkeypatch.setattr(s, "demo_analyst_password_hash", h)
    monkeypatch.setattr(s, "demo_admin_password_hash", h)
    monkeypatch.setattr(s, "jwt_secret", "test-secret")


def test_authenticate_roles():
    assert auth.authenticate("analyst@demo", "pw123") == "analyst"
    assert auth.authenticate("admin@demo", "pw123") == "admin"
    assert auth.authenticate("analyst@demo", "wrong") is None
    assert auth.authenticate("nobody@demo", "pw123") is None


def test_token_roundtrip():
    token = auth.create_token("admin", "admin@demo")
    claims = auth.decode_token(token)
    assert claims["role"] == "admin" and claims["sub"] == "admin@demo"


def test_decode_rejects_garbage_and_wrong_secret():
    with pytest.raises(auth.AuthError):
        auth.decode_token("not.a.token")
    import jwt as pyjwt
    forged = pyjwt.encode({"role": "admin", "sub": "x"}, "other-secret", algorithm="HS256")
    with pytest.raises(auth.AuthError):
        auth.decode_token(forged)


def test_require_role_dependency():
    token = auth.create_token("analyst", "analyst@demo")

    class FakeRequest:
        def __init__(self, header):
            self.headers = {"authorization": header} if header else {}

    assert auth.require_role(FakeRequest(f"Bearer {token}")) == "analyst"
    for bad in (None, "Bearer nope", "Basic abc"):
        with pytest.raises(HTTPException) as e:
            auth.require_role(FakeRequest(bad))
        assert e.value.status_code == 401


def test_decode_rejects_expired_token():
    """Verify decode_token raises AuthError for expired tokens."""
    s = auth.get_settings()
    expired = pyjwt.encode(
        {"sub": "analyst@demo", "role": "analyst",
         "exp": datetime.now(UTC) - timedelta(hours=1)},
        s.jwt_secret,
        algorithm="HS256"
    )
    with pytest.raises(auth.AuthError):
        auth.decode_token(expired)


def test_require_role_via_fastapi_depends():
    """Verify require_role works as FastAPI dependency via Depends()."""
    app = FastAPI()

    @app.get("/protected")
    def protected(role: str = Depends(auth.require_role)):
        return {"role": role}

    client = TestClient(app)
    token = auth.create_token("analyst", "analyst@demo")

    # Valid token should return 200 with correct role
    response = client.get("/protected", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json() == {"role": "analyst"}

    # Missing header should return 401
    response = client.get("/protected")
    assert response.status_code == 401

    # Invalid token should return 401
    response = client.get("/protected", headers={"Authorization": "Bearer invalid"})
    assert response.status_code == 401
