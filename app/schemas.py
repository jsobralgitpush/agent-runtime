import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models import RunStatus

STEP_KEY_PATTERN = r"^[a-z][a-z0-9_-]{0,79}$"


class RetryPolicy(BaseModel):
    max_attempts: int = Field(default=1, ge=1, le=6)
    backoff_seconds: float = Field(default=0.1, ge=0, le=30)


class WorkflowStep(BaseModel):
    key: str = Field(pattern=STEP_KEY_PATTERN)
    type: Literal["llm", "tool"]
    depends_on: list[str] = Field(default_factory=list, max_length=49)
    provider: str | None = None
    fallback_providers: list[str] = Field(default_factory=list, max_length=5)
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
        if self.type == "tool" and self.fallback_providers:
            raise ValueError("Tool steps cannot specify fallback providers")
        if len(self.depends_on) != len(set(self.depends_on)):
            raise ValueError("Step dependencies must not contain duplicates")
        if any(
            re.fullmatch(STEP_KEY_PATTERN, dependency) is None for dependency in self.depends_on
        ):
            raise ValueError("Step dependencies must use valid step keys")
        if any(not name or len(name) > 80 for name in self.fallback_providers):
            raise ValueError("Fallback provider names must contain 1 to 80 characters")
        provider_chain = [self.provider or "fake", *self.fallback_providers]
        if len(provider_chain) != len(set(provider_chain)):
            raise ValueError("Provider fallback chain must not contain duplicates")
        return self


class WorkflowDefinition(BaseModel):
    max_runtime_seconds: float | None = Field(default=None, gt=0, le=3600)
    max_total_tokens: int | None = Field(default=None, gt=0, le=10_000_000)
    max_estimated_cost_usd: float | None = Field(
        default=None, gt=0, le=1_000_000, allow_inf_nan=False
    )
    steps: list[WorkflowStep] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def validate_graph(self) -> "WorkflowDefinition":
        keys = [step.key for step in self.steps]
        if len(keys) != len(set(keys)):
            raise ValueError("Step keys must be unique")
        known_keys = set(keys)
        for step in self.steps:
            if step.key in step.depends_on:
                raise ValueError(f"Step {step.key!r} cannot depend on itself")
            unknown = sorted(set(step.depends_on) - known_keys)
            if unknown:
                raise ValueError(
                    f"Step {step.key!r} depends on unknown steps: {', '.join(unknown)}"
                )
        self.execution_steps()
        self.output_step()
        return self

    def execution_steps(self) -> list[WorkflowStep]:
        """Return a stable topological order, using declaration order as the tie-breaker."""
        pending = list(self.steps)
        completed: set[str] = set()
        ordered: list[WorkflowStep] = []
        while pending:
            for index, step in enumerate(pending):
                if all(dependency in completed for dependency in step.depends_on):
                    ordered.append(step)
                    completed.add(step.key)
                    pending.pop(index)
                    break
            else:
                raise ValueError("Step dependency graph must not contain a cycle")
        return ordered

    def output_step(self) -> WorkflowStep:
        """Select the single DAG terminal, or the last step for legacy linear definitions."""
        if not any(step.depends_on for step in self.steps):
            return self.steps[-1]
        dependency_keys = {dependency for step in self.steps for dependency in step.depends_on}
        terminal_steps = [step for step in self.steps if step.key not in dependency_keys]
        if len(terminal_steps) != 1:
            raise ValueError("Step dependency graph must have exactly one terminal step")
        return terminal_steps[0]


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
    provider: str | None
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
