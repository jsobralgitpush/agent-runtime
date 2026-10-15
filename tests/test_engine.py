import asyncio
from typing import Any

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_ON

from app.config import Settings
from app.engine import (
    AllProvidersFailedError,
    WorkflowCostBudgetExceededError,
    WorkflowCostUnavailableError,
    WorkflowEngine,
    WorkflowTimeBudgetExceededError,
    WorkflowTokenBudgetExceededError,
    resolve_references,
)
from app.metrics import workflow_step_attempts_total
from app.models import RunStatus, Workflow, WorkflowRun
from app.providers import (
    LLMResult,
    PermanentProviderError,
    ProviderRegistry,
    TransientProviderError,
)
from app.schemas import WorkflowDefinition
from app.tools import ToolRegistry
from tests.conftest import TestSession


def test_resolve_references_preserves_types_and_supports_interpolation() -> None:
    inputs = {"name": "Robin", "payload": {"answer": 42}}
    outputs = {"first": [1, 2, 3]}
    assert resolve_references("${input.payload}", inputs, outputs) == {"answer": 42}
    assert resolve_references("Hello ${input.name}", inputs, outputs) == "Hello Robin"
    assert resolve_references("${steps.first.output}", inputs, outputs) == [1, 2, 3]
    runtime = {
        "run_id": "run-1",
        "step_key": "publish",
        "operation_key": "run-1:publish",
    }
    assert resolve_references("${runtime.operation_key}", inputs, outputs, runtime) == (
        "run-1:publish"
    )
    assert resolve_references("Run ${runtime.run_id}", inputs, outputs, runtime) == "Run run-1"
    with pytest.raises(ValueError, match="Unknown workflow reference"):
        resolve_references("${runtime.missing}", inputs, outputs, runtime)


async def test_runtime_context_is_stable_across_tool_retries() -> None:
    received: list[dict[str, object]] = []

    async def capture(value: object) -> object:
        assert isinstance(value, dict)
        received.append(value)
        if len(received) == 1:
            raise RuntimeError("retry once")
        return value

    tools = ToolRegistry()
    tools.register("capture", capture)
    definition = WorkflowDefinition.model_validate(
        {
            "steps": [
                {
                    "key": "publish",
                    "type": "tool",
                    "tool": "capture",
                    "input": {
                        "run_id": "${runtime.run_id}",
                        "step_key": "${runtime.step_key}",
                        "operation_key": "${runtime.operation_key}",
                    },
                    "retry": {"max_attempts": 2, "backoff_seconds": 0},
                }
            ]
        }
    )

    async with TestSession() as session:
        workflow = Workflow(name="Operation key", definition=definition.model_dump(mode="json"))
        session.add(workflow)
        await session.flush()
        run = WorkflowRun(workflow_id=workflow.id, inputs={})
        session.add(run)
        await session.commit()
        result = await WorkflowEngine(Settings(app_env="test"), tools=tools).execute(
            session,
            run,
            definition,
        )

    expected = {
        "run_id": result.id,
        "step_key": "publish",
        "operation_key": f"{result.id}:publish",
    }
    assert result.status == RunStatus.completed
    assert received == [expected, expected]
    assert result.steps[0].input == expected
    assert result.output == expected


async def test_workflow_executes_explicit_dependencies_in_stable_topological_order() -> None:
    definition = WorkflowDefinition.model_validate(
        {
            "steps": [
                {
                    "key": "finish",
                    "type": "tool",
                    "tool": "uppercase",
                    "depends_on": ["middle"],
                    "input": "${steps.middle.output}",
                },
                {"key": "start", "type": "tool", "tool": "echo", "input": "hello"},
                {
                    "key": "middle",
                    "type": "tool",
                    "tool": "uppercase",
                    "depends_on": ["start"],
                    "input": "${steps.start.output}",
                },
            ]
        }
    )

    assert [step.key for step in definition.execution_steps()] == ["start", "middle", "finish"]

    async with TestSession() as session:
        workflow = Workflow(name="DAG", definition=definition.model_dump(mode="json"))
        session.add(workflow)
        await session.flush()
        run = WorkflowRun(workflow_id=workflow.id, inputs={})
        session.add(run)
        await session.commit()
        result = await WorkflowEngine(Settings(app_env="test")).execute(session, run, definition)

    assert result.status == RunStatus.completed
    assert result.output == "HELLO"
    assert [(step.position, step.step_key) for step in result.steps] == [
        (0, "start"),
        (1, "middle"),
        (2, "finish"),
    ]


async def test_dependency_ready_branches_execute_concurrently_and_converge() -> None:
    started: set[str] = set()
    operation_keys: set[str] = set()
    both_started = asyncio.Event()

    async def branch(value: object) -> str:
        assert isinstance(value, dict)
        label = value["label"]
        operation_key = value["operation_key"]
        assert isinstance(label, str)
        assert isinstance(operation_key, str)
        started.add(label)
        operation_keys.add(operation_key)
        if len(started) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=0.1)
        return label.upper()

    async def combine(value: object) -> str:
        assert isinstance(value, dict)
        return f"{value['left']}+{value['right']}"

    tools = ToolRegistry()
    tools.register("branch", branch)
    tools.register("combine", combine)
    definition = WorkflowDefinition.model_validate(
        {
            "max_parallel_steps": 2,
            "steps": [
                {
                    "key": "finish",
                    "type": "tool",
                    "tool": "combine",
                    "depends_on": ["left", "right"],
                    "input": {
                        "left": "${steps.left.output}",
                        "right": "${steps.right.output}",
                    },
                },
                {"key": "start", "type": "tool", "tool": "echo", "input": "root"},
                {
                    "key": "left",
                    "type": "tool",
                    "tool": "branch",
                    "depends_on": ["start"],
                    "input": {
                        "label": "left",
                        "operation_key": "${runtime.operation_key}",
                    },
                },
                {
                    "key": "right",
                    "type": "tool",
                    "tool": "branch",
                    "depends_on": ["start"],
                    "input": {
                        "label": "right",
                        "operation_key": "${runtime.operation_key}",
                    },
                },
            ],
        }
    )

    assert [[step.key for step in batch] for batch in definition.execution_batches()] == [
        ["start"],
        ["left", "right"],
        ["finish"],
    ]

    async with TestSession() as session:
        workflow = Workflow(name="Parallel DAG", definition=definition.model_dump(mode="json"))
        session.add(workflow)
        await session.flush()
        run = WorkflowRun(workflow_id=workflow.id, inputs={})
        session.add(run)
        await session.commit()
        result = await WorkflowEngine(Settings(app_env="test"), tools=tools).execute(
            session, run, definition
        )

    assert result.status == RunStatus.completed
    assert result.output == "LEFT+RIGHT"
    assert started == {"left", "right"}
    assert operation_keys == {f"{result.id}:left", f"{result.id}:right"}
    assert [(step.position, step.step_key) for step in result.steps] == [
        (0, "start"),
        (1, "left"),
        (2, "right"),
        (3, "finish"),
    ]


async def test_parallel_branch_failure_preserves_sibling_result_and_stops_next_wave() -> None:
    started: set[str] = set()
    both_started = asyncio.Event()
    merged = False

    async def branch(value: object) -> str:
        assert isinstance(value, str)
        started.add(value)
        if len(started) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=0.1)
        if value == "right":
            raise RuntimeError("right failed")
        return value.upper()

    async def combine(value: object) -> object:
        nonlocal merged
        merged = True
        return value

    tools = ToolRegistry()
    tools.register("branch", branch)
    tools.register("combine", combine)
    definition = WorkflowDefinition.model_validate(
        {
            "max_parallel_steps": 2,
            "steps": [
                {"key": "start", "type": "tool", "tool": "echo", "input": "root"},
                {
                    "key": "left",
                    "type": "tool",
                    "tool": "branch",
                    "depends_on": ["start"],
                    "input": "left",
                },
                {
                    "key": "right",
                    "type": "tool",
                    "tool": "branch",
                    "depends_on": ["start"],
                    "input": "right",
                },
                {
                    "key": "finish",
                    "type": "tool",
                    "tool": "combine",
                    "depends_on": ["left", "right"],
                },
            ],
        }
    )

    async with TestSession() as session:
        workflow = Workflow(name="Failed branch", definition=definition.model_dump(mode="json"))
        session.add(workflow)
        await session.flush()
        run = WorkflowRun(workflow_id=workflow.id, inputs={})
        session.add(run)
        await session.commit()
        result = await WorkflowEngine(Settings(app_env="test"), tools=tools).execute(
            session, run, definition
        )

    assert result.status == RunStatus.failed
    assert "RuntimeError: right failed" in (result.error or "")
    assert started == {"left", "right"}
    assert merged is False
    assert [(step.step_key, step.status) for step in result.steps] == [
        ("start", RunStatus.completed),
        ("left", RunStatus.completed),
        ("right", RunStatus.failed),
    ]


async def test_parallel_llm_wave_accounts_for_all_token_usage_before_failing() -> None:
    calls = 0

    class MeteredProvider:
        async def complete(self, prompt: str, *, metadata: dict[str, Any]) -> LLMResult:
            nonlocal calls
            calls += 1
            return LLMResult(
                output=f"generated: {prompt}",
                prompt_tokens=2,
                completion_tokens=2,
                estimated_cost_usd=0.01,
            )

    providers = ProviderRegistry()
    providers.register("metered", MeteredProvider())
    definition = WorkflowDefinition.model_validate(
        {
            "max_parallel_steps": 2,
            "max_total_tokens": 5,
            "steps": [
                {"key": "start", "type": "tool", "tool": "echo", "input": "root"},
                {
                    "key": "left",
                    "type": "llm",
                    "provider": "metered",
                    "depends_on": ["start"],
                    "input": "left",
                },
                {
                    "key": "right",
                    "type": "llm",
                    "provider": "metered",
                    "depends_on": ["start"],
                    "input": "right",
                },
                {
                    "key": "finish",
                    "type": "tool",
                    "tool": "echo",
                    "depends_on": ["left", "right"],
                },
            ],
        }
    )

    async with TestSession() as session:
        workflow = Workflow(name="Parallel budget", definition=definition.model_dump(mode="json"))
        session.add(workflow)
        await session.flush()
        run = WorkflowRun(workflow_id=workflow.id, inputs={})
        session.add(run)
        await session.commit()
        result = await WorkflowEngine(Settings(app_env="test"), providers=providers).execute(
            session, run, definition
        )

    assert result.status == RunStatus.failed
    assert "budget of 5 exhausted after consuming 8 tokens" in (result.error or "")
    assert calls == 2
    assert [(step.step_key, step.status) for step in result.steps] == [
        ("start", RunStatus.completed),
        ("left", RunStatus.completed),
        ("right", RunStatus.completed),
    ]
    assert [(step.prompt_tokens, step.completion_tokens) for step in result.steps[1:]] == [
        (2, 2),
        (2, 2),
    ]


async def test_retry_succeeds_after_transient_failure() -> None:
    attempts = 0
    error_counter = workflow_step_attempts_total.labels(step_type="tool", result="error")
    success_counter = workflow_step_attempts_total.labels(step_type="tool", result="success")
    errors_before = error_counter._value.get()
    successes_before = success_counter._value.get()

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
    assert error_counter._value.get() == errors_before + 1
    assert success_counter._value.get() == successes_before + 1


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


async def test_workflow_cost_budget_stops_before_another_llm_call() -> None:
    calls = 0

    class MeteredProvider:
        async def complete(self, prompt: str, *, metadata: dict[str, Any]) -> LLMResult:
            nonlocal calls
            calls += 1
            return LLMResult(
                output=f"generated: {prompt}",
                prompt_tokens=2,
                completion_tokens=3,
                estimated_cost_usd=0.05,
            )

    providers = ProviderRegistry()
    providers.register("metered", MeteredProvider())
    definition = WorkflowDefinition.model_validate(
        {
            "max_estimated_cost_usd": 0.05,
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
        workflow = Workflow(name="Cost budget", definition=definition.model_dump(mode="json"))
        session.add(workflow)
        await session.flush()
        run = WorkflowRun(workflow_id=workflow.id, inputs={})
        session.add(run)
        await session.commit()
        result = await WorkflowEngine(Settings(app_env="test"), providers=providers).execute(
            session, run, definition
        )

    assert result.status == RunStatus.failed
    assert WorkflowCostBudgetExceededError.__name__ in (result.error or "")
    assert "budget of $0.05 exhausted after an estimated $0.05" in (result.error or "")
    assert calls == 1
    assert len(result.steps) == 2
    assert result.steps[0].status == RunStatus.completed
    assert result.steps[0].estimated_cost_usd == 0.05
    assert result.steps[1].status == RunStatus.completed
    assert result.steps[1].step_type == "tool"


async def test_workflow_cost_budget_records_a_single_call_overshoot() -> None:
    class MeteredProvider:
        async def complete(self, prompt: str, *, metadata: dict[str, Any]) -> LLMResult:
            return LLMResult(
                output="costly response",
                prompt_tokens=3,
                completion_tokens=4,
                estimated_cost_usd=0.06,
            )

    providers = ProviderRegistry()
    providers.register("metered", MeteredProvider())
    definition = WorkflowDefinition.model_validate(
        {
            "max_estimated_cost_usd": 0.05,
            "steps": [{"key": "generate", "type": "llm", "provider": "metered"}],
        }
    )

    async with TestSession() as session:
        workflow = Workflow(name="Cost overshoot", definition=definition.model_dump(mode="json"))
        session.add(workflow)
        await session.flush()
        run = WorkflowRun(workflow_id=workflow.id, inputs={})
        session.add(run)
        await session.commit()
        result = await WorkflowEngine(Settings(app_env="test"), providers=providers).execute(
            session, run, definition
        )

    assert result.status == RunStatus.failed
    assert "budget of $0.05 exhausted after an estimated $0.06" in (result.error or "")
    assert result.steps[0].status == RunStatus.completed
    assert result.steps[0].output == "costly response"


async def test_workflow_cost_budget_fails_closed_when_cost_is_unknown() -> None:
    class UnpricedProvider:
        async def complete(self, prompt: str, *, metadata: dict[str, Any]) -> LLMResult:
            return LLMResult(
                output="unpriced response",
                prompt_tokens=1,
                completion_tokens=1,
                estimated_cost_usd=None,
            )

    providers = ProviderRegistry()
    providers.register("unpriced", UnpricedProvider())
    definition = WorkflowDefinition.model_validate(
        {
            "max_estimated_cost_usd": 1,
            "steps": [{"key": "generate", "type": "llm", "provider": "unpriced"}],
        }
    )

    async with TestSession() as session:
        workflow = Workflow(name="Unknown cost", definition=definition.model_dump(mode="json"))
        session.add(workflow)
        await session.flush()
        run = WorkflowRun(workflow_id=workflow.id, inputs={})
        session.add(run)
        await session.commit()
        result = await WorkflowEngine(Settings(app_env="test"), providers=providers).execute(
            session, run, definition
        )

    assert result.status == RunStatus.failed
    assert WorkflowCostUnavailableError.__name__ in (result.error or "")
    assert "provider 'unpriced'" in (result.error or "")
    assert result.steps[0].status == RunStatus.completed
    assert result.steps[0].estimated_cost_usd is None


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


async def test_llm_step_does_not_retry_permanent_provider_failure() -> None:
    calls = 0

    class InvalidRequestProvider:
        async def complete(self, prompt: str, *, metadata: dict[str, Any]) -> LLMResult:
            nonlocal calls
            calls += 1
            raise PermanentProviderError("invalid request")

    providers = ProviderRegistry()
    providers.register("invalid", InvalidRequestProvider())
    definition = WorkflowDefinition.model_validate(
        {
            "steps": [
                {
                    "key": "generate",
                    "type": "llm",
                    "provider": "invalid",
                    "input": "hello",
                    "retry": {"max_attempts": 3, "backoff_seconds": 0},
                }
            ]
        }
    )

    async with TestSession() as session:
        workflow = Workflow(name="Permanent failure", definition=definition.model_dump(mode="json"))
        session.add(workflow)
        await session.flush()
        run = WorkflowRun(workflow_id=workflow.id, inputs={})
        session.add(run)
        await session.commit()
        result = await WorkflowEngine(Settings(app_env="test"), providers=providers).execute(
            session, run, definition
        )

    assert result.status == RunStatus.failed
    assert result.steps[0].attempts == 1
    assert calls == 1


async def test_llm_step_retries_transient_provider_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    retry_delays: list[float] = []

    async def record_retry_delay(delay: float, deadline: float | None) -> None:
        assert deadline is None
        retry_delays.append(delay)

    monkeypatch.setattr("app.engine.wait_for_retry", record_retry_delay)

    class RecoveringProvider:
        async def complete(self, prompt: str, *, metadata: dict[str, Any]) -> LLMResult:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TransientProviderError(
                    "temporarily unavailable",
                    retry_after_seconds=120,
                )
            return LLMResult(
                output="recovered",
                prompt_tokens=1,
                completion_tokens=1,
                estimated_cost_usd=0.0,
            )

    providers = ProviderRegistry()
    providers.register("recovering", RecoveringProvider())
    definition = WorkflowDefinition.model_validate(
        {
            "steps": [
                {
                    "key": "generate",
                    "type": "llm",
                    "provider": "recovering",
                    "input": "hello",
                    "retry": {"max_attempts": 2, "backoff_seconds": 0},
                }
            ]
        }
    )

    async with TestSession() as session:
        workflow = Workflow(name="Transient failure", definition=definition.model_dump(mode="json"))
        session.add(workflow)
        await session.flush()
        run = WorkflowRun(workflow_id=workflow.id, inputs={})
        session.add(run)
        await session.commit()
        result = await WorkflowEngine(
            Settings(app_env="test", provider_retry_after_max_seconds=5),
            providers=providers,
        ).execute(session, run, definition)

    assert result.status == RunStatus.completed
    assert result.output == "recovered"
    assert result.steps[0].attempts == 2
    assert calls == 2
    assert retry_delays == [5]


async def test_provider_chain_uses_longest_retry_after_hint() -> None:
    class ThrottledProvider:
        def __init__(self, retry_after_seconds: float) -> None:
            self.retry_after_seconds = retry_after_seconds

        async def complete(self, prompt: str, *, metadata: dict[str, Any]) -> LLMResult:
            raise TransientProviderError(
                "temporarily unavailable",
                retry_after_seconds=self.retry_after_seconds,
            )

    providers = ProviderRegistry()
    providers.register("primary", ThrottledProvider(3))
    providers.register("backup", ThrottledProvider(8))
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
    engine = WorkflowEngine(Settings(app_env="test"), providers=providers)

    with pytest.raises(AllProvidersFailedError) as captured:
        await engine._dispatch(definition.steps[0], "hello", "run-1")

    assert captured.value.retry_after_seconds == 8


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
