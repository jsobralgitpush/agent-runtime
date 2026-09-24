from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models import RunStatus


class RetryPolicy(BaseModel):
    max_attempts: int = Field(default=1, ge=1, le=6)
    backoff_seconds: float = Field(default=0.1, ge=0, le=30)


class WorkflowStep(BaseModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,79}$")
    type: Literal["llm", "tool"]
    provider: str | None = None
    tool: str | None = None
    input: Any = None
    timeout_seconds: float | None = Field(default=None, gt=0, le=300)
    retry: RetryPolicy = Field(default_factory=RetryPolicy)

    @model_validator(mode="after")
    def validate_target(self) -> "WorkflowStep":
        if self.type == "llm" and self.tool is not None:
            raise ValueError("LLM steps cannot specify a tool")
        if self.type == "tool" and not self.tool:
            raise ValueError("Tool steps must specify a tool")
        return self


class WorkflowDefinition(BaseModel):
    steps: list[WorkflowStep] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def unique_keys(self) -> "WorkflowDefinition":
        keys = [step.key for step in self.steps]
        if len(keys) != len(set(keys)):
            raise ValueError("Step keys must be unique")
        return self


class WorkflowCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    definition: WorkflowDefinition


class WorkflowRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str | None
    definition: WorkflowDefinition
    created_at: datetime


class RunCreate(BaseModel):
    inputs: dict[str, Any] = Field(default_factory=dict)


class StepRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    step_key: str
    position: int
    step_type: str
    status: RunStatus
    attempts: int
    input: Any | None
    output: Any | None
    error: str | None
    latency_ms: float | None
    prompt_tokens: int | None
    completion_tokens: int | None
    estimated_cost_usd: float | None
    started_at: datetime | None
    completed_at: datetime | None


class RunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    workflow_id: str
    idempotency_key: str | None
    status: RunStatus
    inputs: dict[str, Any]
    output: Any | None
    error: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    lease_owner: str | None
    lease_expires_at: datetime | None
    heartbeat_at: datetime | None
    steps: list[StepRunRead] = Field(default_factory=list)


class HealthRead(BaseModel):
    status: Literal["ok"] = "ok"
