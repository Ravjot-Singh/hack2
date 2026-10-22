"""Application settings, loaded from environment variables / .env."""

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parent.parent
TENANTS_DIR = ROOT_DIR / "tenants"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT_DIR / ".env", extra="ignore")

    database_url: str = f"sqlite:///{ROOT_DIR / 'local.db'}"
    public_base_url: str = ""
    webhook_secret: str = "change-me"

    admin_user: str = "admin"
    admin_password: str = "change-me"

    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"
    llm_enabled: bool = False
    llm_max_calls_per_minute: int = 20
    llm_max_calls_per_day: int = 500

    @field_validator("database_url")
    @classmethod
    def use_psycopg3(cls, v: str) -> str:
        # Neon/Render hand out postgres:// or postgresql:// URLs; SQLAlchemy needs the driver named.
        for prefix in ("postgres://", "postgresql://"):
            if v.startswith(prefix):
                return "postgresql+psycopg://" + v[len(prefix):]
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
