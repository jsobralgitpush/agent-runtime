import asyncio
from typing import Any

from app.config import Settings
from app.engine import AllProvidersFailedError, WorkflowEngine, resolve_references
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
