import asyncio
from typing import Any

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_ON

from app.config import Settings
from app.engine import (
    AllProvidersFailedError,
    WorkflowEngine,
    WorkflowTimeBudgetExceededError,
    WorkflowTokenBudgetExceededError,
    resolve_references,
)
from app.models import RunStatus, Workflow, WorkflowRun
from app.providers import LLMResult, ProviderRegistry
from app.schemas import WorkflowDefinition
from app.tools import ToolRegistry
from tests.conftest import TestSession


def test_resolve_references_preserves_types_and_supports_interpolation() -> None:
    inputs = {"name": "Robin", "payload": {"answer": 42}}
    outputs = {"first": [1, 2, 3]}
    assert resolve_references("${input.payload}", inputs, outputs) == {"answer": 42}
    assert resolve_references("Hello ${input.name}", inputs, outputs) == "Hello Robin"
    assert resolve_references("${steps.first.output}", inputs, outputs) == [1, 2, 3]


async def test_retry_succeeds_after_transient_failure() -> None:
    attempts = 0

    async def flaky(value: object) -> object:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transient")
        return value

    tools = ToolRegistry()
    tools.register("flaky", flaky)
    definition = WorkflowDefinition.model_validate(
        {
            "steps": [
                {
                    "key": "retry",
                    "type": "tool",
                    "tool": "flaky",
                    "input": "ok",
                    "retry": {"max_attempts": 2, "backoff_seconds": 0},
                }
            ]
        }
    )
    async with TestSession() as session:
        workflow = Workflow(name="Retry", definition=definition.model_dump(mode="json"))
        session.add(workflow)
        await session.flush()
        run = WorkflowRun(workflow_id=workflow.id, inputs={})
        session.add(run)
        await session.commit()
        result = await WorkflowEngine(Settings(app_env="test"), tools=tools).execute(
            session, run, definition
        )
    assert result.status == RunStatus.completed
    assert result.steps[0].attempts == 2
    assert result.steps[0].output == "ok"


async def test_timeout_marks_step_and_run_failed() -> None:
    async def slow(value: object) -> object:
        await asyncio.sleep(0.05)
        return value

    tools = ToolRegistry()
    tools.register("slow", slow)
    definition = WorkflowDefinition.model_validate(
        {
            "steps": [
                {
                    "key": "slow",
                    "type": "tool",
                    "tool": "slow",
                    "input": "late",
                    "timeout_seconds": 0.001,
                }
            ]
        }
    )
    async with TestSession() as session:
        workflow = Workflow(name="Timeout", definition=definition.model_dump(mode="json"))
        session.add(workflow)
        await session.flush()
        run = WorkflowRun(workflow_id=workflow.id, inputs={})
        session.add(run)
        await session.commit()
        result = await WorkflowEngine(Settings(app_env="test"), tools=tools).execute(
            session, run, definition
        )
    assert result.status == RunStatus.failed
    assert result.steps[0].status == RunStatus.failed
    assert "TimeoutError" in (result.steps[0].error or "")


async def test_workflow_time_budget_stops_active_step() -> None:
    async def slow(value: object) -> object:
        await asyncio.sleep(0.2)
        return value

    tools = ToolRegistry()
    tools.register("slow", slow)
    definition = WorkflowDefinition.model_validate(
        {
            "max_runtime_seconds": 0.05,
            "steps": [
                {
                    "key": "slow",
                    "type": "tool",
                    "tool": "slow",
                    "input": "late",
                    "timeout_seconds": 1,
                    "retry": {"max_attempts": 2, "backoff_seconds": 0},
                }
            ],
        }
    )
    async with TestSession() as session:
        workflow = Workflow(name="Budget", definition=definition.model_dump(mode="json"))
        session.add(workflow)
        await session.flush()
        run = WorkflowRun(workflow_id=workflow.id, inputs={})
        session.add(run)
        await session.commit()
        result = await WorkflowEngine(Settings(app_env="test"), tools=tools).execute(
            session, run, definition
        )

    assert result.status == RunStatus.failed
    assert WorkflowTimeBudgetExceededError.__name__ in (result.error or "")
    assert len(result.steps) == 1
    assert result.steps[0].status == RunStatus.failed
    assert WorkflowTimeBudgetExceededError.__name__ in (result.steps[0].error or "")
    assert result.steps[0].attempts == 1


async def test_workflow_time_budget_includes_retry_backoff() -> None:
    attempts = 0

    async def failing(value: object) -> object:
        nonlocal attempts
        attempts += 1
        raise RuntimeError(f"failure: {value}")

    tools = ToolRegistry()
    tools.register("failing", failing)
    definition = WorkflowDefinition.model_validate(
        {
            "max_runtime_seconds": 0.05,
            "steps": [
                {
                    "key": "retry",
                    "type": "tool",
                    "tool": "failing",
                    "input": "retry",
                    "retry": {"max_attempts": 3, "backoff_seconds": 0.2},
                }
            ],
        }
    )
    async with TestSession() as session:
        workflow = Workflow(name="Backoff budget", definition=definition.model_dump(mode="json"))
        session.add(workflow)
        await session.flush()
        run = WorkflowRun(workflow_id=workflow.id, inputs={})
        session.add(run)
        await session.commit()
        result = await WorkflowEngine(Settings(app_env="test"), tools=tools).execute(
            session, run, definition
        )

    assert result.status == RunStatus.failed
    assert WorkflowTimeBudgetExceededError.__name__ in (result.error or "")
    assert WorkflowTimeBudgetExceededError.__name__ in (result.steps[0].error or "")
    assert result.steps[0].attempts == 1
    assert attempts == 1


async def test_workflow_token_budget_stops_before_another_llm_call() -> None:
    calls = 0

    class MeteredProvider:
        async def complete(self, prompt: str, *, metadata: dict[str, Any]) -> LLMResult:
            nonlocal calls
            calls += 1
            return LLMResult(
                output=f"generated: {prompt}",
                prompt_tokens=2,
                completion_tokens=3,
                estimated_cost_usd=None,
            )

    providers = ProviderRegistry()
    providers.register("metered", MeteredProvider())
    definition = WorkflowDefinition.model_validate(
        {
            "max_total_tokens": 5,
            "steps": [
                {"key": "first", "type": "llm", "provider": "metered", "input": "one"},
                {
                    "key": "transform",
                    "type": "tool",
                    "tool": "uppercase",
                    "input": "${steps.first.output}",
                },
                {"key": "second", "type": "llm", "provider": "metered", "input": "two"},
            ],
        }
    )

    async with TestSession() as session:
        workflow = Workflow(name="Token budget", definition=definition.model_dump(mode="json"))
        session.add(workflow)
        await session.flush()
        run = WorkflowRun(workflow_id=workflow.id, inputs={})
        session.add(run)
        await session.commit()
        result = await WorkflowEngine(Settings(app_env="test"), providers=providers).execute(
            session, run, definition
        )

    assert result.status == RunStatus.failed
    assert WorkflowTokenBudgetExceededError.__name__ in (result.error or "")
    assert calls == 1
    assert len(result.steps) == 2
    assert result.steps[0].status == RunStatus.completed
    assert result.steps[0].prompt_tokens == 2
    assert result.steps[0].completion_tokens == 3
    assert result.steps[1].status == RunStatus.completed
    assert result.steps[1].step_type == "tool"


async def test_workflow_token_budget_records_a_single_call_overshoot() -> None:
    class MeteredProvider:
        async def complete(self, prompt: str, *, metadata: dict[str, Any]) -> LLMResult:
            return LLMResult(
                output="larger than predicted",
                prompt_tokens=3,
                completion_tokens=4,
                estimated_cost_usd=None,
            )

    providers = ProviderRegistry()
    providers.register("metered", MeteredProvider())
    definition = WorkflowDefinition.model_validate(
        {
            "max_total_tokens": 5,
            "steps": [{"key": "generate", "type": "llm", "provider": "metered", "input": "one"}],
        }
    )

    async with TestSession() as session:
        workflow = Workflow(name="Token overshoot", definition=definition.model_dump(mode="json"))
        session.add(workflow)
        await session.flush()
        run = WorkflowRun(workflow_id=workflow.id, inputs={})
        session.add(run)
        await session.commit()
        result = await WorkflowEngine(Settings(app_env="test"), providers=providers).execute(
            session, run, definition
        )

    assert result.status == RunStatus.failed
    assert "budget of 5 exhausted after consuming 7 tokens" in (result.error or "")
    assert result.steps[0].status == RunStatus.completed
    assert result.steps[0].output == "larger than predicted"


async def test_llm_step_falls_back_and_records_selected_provider() -> None:
    class FailingProvider:
        async def complete(self, prompt: str, *, metadata: dict[str, Any]) -> LLMResult:
            raise RuntimeError("primary unavailable")

    class BackupProvider:
        async def complete(self, prompt: str, *, metadata: dict[str, Any]) -> LLMResult:
            return LLMResult(
                output=f"backup: {prompt}",
                prompt_tokens=1,
                completion_tokens=2,
                estimated_cost_usd=0.01,
            )

    providers = ProviderRegistry()
    providers.register("primary", FailingProvider())
    providers.register("backup", BackupProvider())
    definition = WorkflowDefinition.model_validate(
        {
            "steps": [
                {
                    "key": "generate",
                    "type": "llm",
                    "provider": "primary",
                    "fallback_providers": ["backup"],
                    "input": "hello",
                }
            ]
        }
    )

    async with TestSession() as session:
        workflow = Workflow(name="Fallback", definition=definition.model_dump(mode="json"))
        session.add(workflow)
        await session.flush()
        run = WorkflowRun(workflow_id=workflow.id, inputs={})
        session.add(run)
        await session.commit()
        result = await WorkflowEngine(Settings(app_env="test"), providers=providers).execute(
            session, run, definition
        )

    assert result.status == RunStatus.completed
    assert result.output == "backup: hello"
    assert result.steps[0].provider == "backup"
    assert result.steps[0].attempts == 1


async def test_llm_step_fails_after_exhausting_provider_chain() -> None:
    class FailingProvider:
        async def complete(self, prompt: str, *, metadata: dict[str, Any]) -> LLMResult:
            raise RuntimeError("unavailable")

    providers = ProviderRegistry()
    providers.register("primary", FailingProvider())
    providers.register("backup", FailingProvider())
    definition = WorkflowDefinition.model_validate(
        {
            "steps": [
                {
                    "key": "generate",
                    "type": "llm",
                    "provider": "primary",
                    "fallback_providers": ["backup"],
                    "input": "hello",
                }
            ]
        }
    )

    async with TestSession() as session:
        workflow = Workflow(name="Failure", definition=definition.model_dump(mode="json"))
        session.add(workflow)
        await session.flush()
        run = WorkflowRun(workflow_id=workflow.id, inputs={})
        session.add(run)
        await session.commit()
        result = await WorkflowEngine(Settings(app_env="test"), providers=providers).execute(
            session, run, definition
        )

    assert result.status == RunStatus.failed
    assert AllProvidersFailedError.__name__ in (result.error or "")
    assert "primary, backup" in (result.error or "")
    assert result.steps[0].provider is None


async def test_workflow_spans_are_nested_and_exclude_payloads() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider(sampler=ALWAYS_ON)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    definition = WorkflowDefinition.model_validate(
        {
            "steps": [
                {
                    "key": "generate",
                    "type": "llm",
                    "provider": "fake",
                    "input": "private: ${input.secret}",
                }
            ]
        }
    )

    async with TestSession() as session:
        workflow = Workflow(name="Traced", definition=definition.model_dump(mode="json"))
        session.add(workflow)
        await session.flush()
        run = WorkflowRun(workflow_id=workflow.id, inputs={"secret": "do-not-export"})
        session.add(run)
        await session.commit()
        result = await WorkflowEngine(
            Settings(app_env="test"),
            tracer=provider.get_tracer("tests"),
        ).execute(session, run, definition)

    spans = {span.name: span for span in exporter.get_finished_spans()}
    run_span = spans["workflow.run"]
    step_span = spans["workflow.step"]
    assert step_span.parent is not None
    assert step_span.parent.span_id == run_span.context.span_id
    assert run_span.attributes is not None
    assert run_span.attributes["agent_runtime.run.id"] == result.id
    assert run_span.attributes["agent_runtime.run.status"] == "completed"
    assert step_span.attributes is not None
    assert step_span.attributes["agent_runtime.step.key"] == "generate"
    assert step_span.attributes["agent_runtime.step.provider"] == "fake"
    assert "do-not-export" not in repr([span.attributes for span in spans.values()])
    assert all(not span.events for span in spans.values())
    provider.shutdown()
