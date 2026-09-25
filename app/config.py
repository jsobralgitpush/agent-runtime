import os
import socket
from functools import lru_cache
from typing import Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "agent-runtime"
    app_env: str = "development"
    database_url: str = "sqlite+aiosqlite:///./agent-runtime.db"
    log_level: str = "INFO"
    default_step_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    max_step_retries: int = Field(default=5, ge=0, le=10)
    worker_poll_interval_seconds: float = Field(default=1.0, gt=0, le=60)
    worker_id: str = Field(default_factory=_default_worker_id, min_length=1, max_length=255)
    worker_lease_seconds: int = Field(default=60, ge=5, le=3600)
    worker_heartbeat_seconds: float = Field(default=15.0, gt=0, le=300)
    worker_recovery_batch_size: int = Field(default=100, ge=1, le=1000)

    @model_validator(mode="after")
    def heartbeat_precedes_lease_expiry(self) -> Self:
        if self.worker_heartbeat_seconds >= self.worker_lease_seconds:
            raise ValueError("worker heartbeat interval must be shorter than the lease")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
