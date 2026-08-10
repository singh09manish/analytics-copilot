"""The container has no .env and no key file on disk, so both have to work from
environment variables alone."""
import pytest

from copilot.config import Settings
from copilot.snowflake_client import _load_private_key


def _pem() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


def test_load_private_key_prefers_pem_over_path():
    der = _load_private_key("does/not/exist.p8", pem=_pem())
    assert isinstance(der, bytes) and len(der) > 100


def test_load_private_key_falls_back_to_path(tmp_path):
    p = tmp_path / "k.p8"
    p.write_text(_pem())
    assert isinstance(_load_private_key(str(p)), bytes)


def test_load_private_key_reports_both_sources_when_missing():
    with pytest.raises(FileNotFoundError, match="SNOWFLAKE_PRIVATE_KEY_PEM"):
        _load_private_key("nope/missing.p8")


def test_cors_origin_list_splits_and_strips():
    s = Settings(cors_allow_origins="https://a.example , https://b.example")
    assert s.cors_origin_list() == ["https://a.example", "https://b.example"]


def test_cors_origin_list_default_is_local_dev():
    assert Settings().cors_origin_list() == ["http://localhost:5173"]


def test_middleware_cors_wired_from_settings():
    """Verify CORS middleware is configured with origins from settings, not hardcoded."""
    from copilot.api.main import app
    from copilot.config import get_settings

    # Find the CORSMiddleware in the app's middleware stack
    cors_middleware = None
    for middleware in app.user_middleware:
        if middleware.cls.__name__ == "CORSMiddleware":
            cors_middleware = middleware
            break
    assert cors_middleware is not None, "CORSMiddleware not found in app middleware"

    # Verify the middleware was configured with origins from settings
    expected_origins = get_settings().cors_origin_list()
    actual_origins = cors_middleware.kwargs.get("allow_origins", [])

    # Starlette stores allow_origins as a tuple or list depending on version
    actual_origins = list(actual_origins) if actual_origins else []
    assert actual_origins == expected_origins, (
        f"CORS middleware origins {actual_origins} do not match "
        f"settings {expected_origins}"
    )
