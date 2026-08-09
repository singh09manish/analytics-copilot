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


@lru_cache
def get_settings() -> Settings:
    return Settings()
