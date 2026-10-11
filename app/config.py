import os
import socket
from functools import lru_cache
from typing import Self

from pydantic import Field, SecretStr, model_validator
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
    worker_metrics_host: str = Field(default="127.0.0.1", min_length=1, max_length=255)
    worker_metrics_port: int | None = Field(default=None, ge=1, le=65535)
    otel_exporter_otlp_traces_endpoint: str | None = Field(default=None, min_length=1)
    otel_service_name: str = Field(default="agent-runtime", min_length=1, max_length=255)
    openai_api_key: SecretStr | None = None
    openai_model: str = Field(default="gpt-5-mini", min_length=1, max_length=120)
    openai_base_url: str = Field(default="https://api.openai.com/v1", min_length=1)
    openai_input_cost_per_million_tokens_usd: float | None = Field(
        default=None, ge=0, allow_inf_nan=False
    )
    openai_output_cost_per_million_tokens_usd: float | None = Field(
        default=None, ge=0, allow_inf_nan=False
    )
    anthropic_api_key: SecretStr | None = None
    anthropic_model: str = Field(default="claude-sonnet-5", min_length=1, max_length=120)
    anthropic_base_url: str = Field(default="https://api.anthropic.com/v1", min_length=1)
    anthropic_max_tokens: int = Field(default=4096, ge=1, le=128_000)
    anthropic_input_cost_per_million_tokens_usd: float | None = Field(
        default=None, ge=0, allow_inf_nan=False
    )
    anthropic_output_cost_per_million_tokens_usd: float | None = Field(
        default=None, ge=0, allow_inf_nan=False
    )

    @model_validator(mode="after")
    def validate_operational_settings(self) -> Self:
        if self.worker_heartbeat_seconds >= self.worker_lease_seconds:
            raise ValueError("worker heartbeat interval must be shorter than the lease")
        for provider, input_rate, output_rate in (
            (
                "OpenAI",
                self.openai_input_cost_per_million_tokens_usd,
                self.openai_output_cost_per_million_tokens_usd,
            ),
            (
                "Anthropic",
                self.anthropic_input_cost_per_million_tokens_usd,
                self.anthropic_output_cost_per_million_tokens_usd,
            ),
        ):
            if (input_rate is None) != (output_rate is None):
                raise ValueError(
                    f"{provider} input and output cost rates must be configured together"
                )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
