from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "agent-runtime"
    app_env: str = "development"
    database_url: str = "sqlite+aiosqlite:///./agent-runtime.db"
    log_level: str = "INFO"
    default_step_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    max_step_retries: int = Field(default=5, ge=0, le=10)
    worker_poll_interval_seconds: float = Field(default=1.0, gt=0, le=60)


@lru_cache
def get_settings() -> Settings:
    return Settings()
