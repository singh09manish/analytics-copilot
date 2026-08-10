from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=REPO_ROOT / ".env", extra="ignore")

    anthropic_api_key: str = ""
    app_model: str = "claude-sonnet-5"
    snowflake_account: str = ""
    snowflake_user: str = "COPILOT_SVC"
    snowflake_private_key_path: str = "secrets/copilot_svc_key.p8"
    snowflake_warehouse: str = "COPILOT_WH"
    snowflake_database: str = "MEDTECH_ANALYTICS"
    snowflake_role: str = "COPILOT_APP_RO"
    jwt_secret: str = "dev-secret-change-me"
    jwt_ttl_hours: int = 8
    demo_analyst_password_hash: str = ""
    demo_admin_password_hash: str = ""
    use_mcp: bool = True
    # Container deployments have no key file on disk; the PEM arrives from Secrets
    # Manager as an env var. Empty means "use snowflake_private_key_path" (local dev).
    snowflake_private_key_pem: str = ""
    # Comma-separated. Same-origin behind CloudFront makes this moot in AWS, but it
    # stays configurable so a split-origin deployment does not need a code change.
    cors_allow_origins: str = "http://localhost:5173"
    aws_region: str = "us-east-1"

    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_allow_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
