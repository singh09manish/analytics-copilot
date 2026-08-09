"""Demo auth: two fixed accounts, bcrypt hashes from settings, HS256 JWTs."""
from datetime import UTC, datetime, timedelta

import bcrypt
import jwt
from fastapi import HTTPException, Request

from copilot.config import get_settings


class AuthError(Exception):
    pass


_ACCOUNTS = {"analyst@demo": ("analyst", "demo_analyst_password_hash"),
             "admin@demo": ("admin", "demo_admin_password_hash")}


def authenticate(email: str, password: str) -> str | None:
    entry = _ACCOUNTS.get(email.strip().lower())
    if entry is None:
        return None
    role, hash_field = entry
    stored = getattr(get_settings(), hash_field)
    if not stored:
        return None
    if bcrypt.checkpw(password.encode(), stored.encode()):
        return role
    return None


def create_token(role: str, email: str) -> str:
    s = get_settings()
    payload = {"sub": email, "role": role,
               "exp": datetime.now(UTC) + timedelta(hours=s.jwt_ttl_hours)}
    return jwt.encode(payload, s.jwt_secret, algorithm="HS256")


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError as e:
        raise AuthError(str(e)) from e


def require_role(request: Request) -> str:
    """FastAPI dependency: returns 'analyst' | 'admin' or raises 401."""
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    try:
        return decode_token(header.removeprefix("Bearer "))["role"]
    except AuthError as e:
        raise HTTPException(status_code=401, detail=f"invalid token: {e}") from e
