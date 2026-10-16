import json
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.providers import (
    AnthropicProvider,
    FakeLLMProvider,
    OpenAIProvider,
    PermanentProviderError,
    ProviderRegistry,
    TransientProviderError,
    _parse_retry_after,
    build_provider_registry,
)
from app.schemas import WorkflowDefinition
from app.tools import ToolRegistry, echo, extract_field, uppercase


async def test_builtin_tools() -> None:
    assert await echo({"ok": True}) == {"ok": True}
    assert await uppercase("hello") == "HELLO"
    assert await extract_field({"object": {"answer": 42}, "field": "answer"}) == 42


async def test_builtin_tools_reject_invalid_inputs() -> None:
    with pytest.raises(ValueError, match="string"):
        await uppercase(42)
    with pytest.raises(ValueError, match="object and field"):
        await extract_field({})
    with pytest.raises(ValueError, match="mapping"):
        await extract_field({"object": [], "field": "answer"})


def test_tool_registry_guards_names() -> None:
    registry = ToolRegistry()
    assert registry.get("echo") is echo
    with pytest.raises(ValueError, match="Unknown tool"):
        registry.get("missing")
    with pytest.raises(ValueError, match="already registered"):
        registry.register("echo", echo)


def test_workflow_rejects_invalid_provider_fallback_chains() -> None:
    with pytest.raises(ValidationError, match="must not contain duplicates"):
        WorkflowDefinition.model_validate(
            {
                "steps": [
                    {
                        "key": "generate",
                        "type": "llm",
                        "provider": "primary",
                        "fallback_providers": ["primary"],
                    }
                ]
            }
        )

    with pytest.raises(ValidationError, match="Tool steps cannot specify"):
        WorkflowDefinition.model_validate(
            {
                "steps": [
                    {
                        "key": "publish",
                        "type": "tool",
                        "tool": "echo",
                        "fallback_providers": ["backup"],
                    }
                ]
            }
        )

    with pytest.raises(ValidationError, match="Tool steps cannot specify a provider timeout"):
        WorkflowDefinition.model_validate(
            {
                "steps": [
                    {
                        "key": "publish",
                        "type": "tool",
                        "tool": "echo",
                        "provider_timeout_seconds": 1,
                    }
                ]
            }
        )


@pytest.mark.parametrize(
    ("steps", "message"),
    [
        (
            [
                {
                    "key": "publish",
                    "type": "tool",
                    "tool": "echo",
                    "depends_on": ["missing"],
                }
            ],
            "depends on unknown steps: missing",
        ),
        (
            [
                {
                    "key": "publish",
                    "type": "tool",
                    "tool": "echo",
                    "depends_on": ["publish"],
                }
            ],
            "cannot depend on itself",
        ),
        (
            [
                {
                    "key": "first",
                    "type": "tool",
                    "tool": "echo",
                    "depends_on": ["second"],
                },
                {
                    "key": "second",
                    "type": "tool",
                    "tool": "echo",
                    "depends_on": ["first"],
                },
            ],
            "must not contain a cycle",
        ),
        (
            [
                {
                    "key": "publish",
                    "type": "tool",
                    "tool": "echo",
                    "depends_on": ["draft", "draft"],
                },
                {"key": "draft", "type": "tool", "tool": "echo"},
            ],
            "must not contain duplicates",
        ),
        (
            [
                {"key": "first", "type": "tool", "tool": "echo"},
                {
                    "key": "second",
                    "type": "tool",
                    "tool": "echo",
                    "depends_on": ["first"],
                },
                {
                    "key": "independent",
                    "type": "tool",
                    "tool": "echo",
                    "depends_on": ["first"],
                },
            ],
            "must have exactly one terminal step",
        ),
    ],
)
def test_workflow_rejects_invalid_step_dependencies(
    steps: list[dict[str, object]], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        WorkflowDefinition.model_validate({"steps": steps})


def test_parallel_execution_requires_an_explicit_dependency_graph() -> None:
    with pytest.raises(ValidationError, match="requires explicit step dependencies"):
        WorkflowDefinition.model_validate(
            {
                "max_parallel_steps": 2,
                "steps": [{"key": "only", "type": "tool", "tool": "echo"}],
            }
        )


def test_approval_step_is_an_exclusive_dependency_ready_wave() -> None:
    definition = WorkflowDefinition.model_validate(
        {
            "max_parallel_steps": 3,
            "steps": [
                {"key": "before", "type": "tool", "tool": "echo"},
                {"key": "review", "type": "approval"},
                {"key": "after", "type": "tool", "tool": "echo"},
                {
                    "key": "finish",
                    "type": "tool",
                    "tool": "echo",
                    "depends_on": ["before", "review", "after"],
                },
            ],
        }
    )

    assert [[step.key for step in batch] for batch in definition.execution_batches()] == [
        ["before"],
        ["review"],
        ["after"],
        ["finish"],
    ]


async def test_provider_registry_and_fake_provider() -> None:
    registry = ProviderRegistry()
    provider = registry.get("fake")
    first = await provider.complete("same prompt", metadata={})
    second = await provider.complete("same prompt", metadata={})
    assert first == second
    assert first.estimated_cost_usd == 0

    with pytest.raises(ValueError, match="Unknown LLM provider"):
        registry.get("missing")
    with pytest.raises(ValueError, match="already registered"):
        registry.register("fake", FakeLLMProvider())


async def test_openai_provider_uses_responses_api_and_normalizes_result() -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://api.openai.test/v1/responses"
        assert request.headers["Authorization"] == "Bearer test-secret"
        assert request.headers["X-Client-Request-Id"] == "run-1:draft"
        assert json.loads(request.content) == {
            "model": "test-model",
            "input": "Hello",
            "store": False,
            "metadata": {"run_id": "run-1", "step": "draft"},
        }
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": "Hello back", "annotations": []}
                        ],
                    }
                ],
                "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = OpenAIProvider(
            api_key="test-secret",
            model="test-model",
            base_url="https://api.openai.test/v1/",
            input_cost_per_million_tokens_usd=2,
            output_cost_per_million_tokens_usd=4,
            client=client,
        )
        result = await provider.complete(
            "Hello", metadata={"run_id": "run-1", "step": "draft", "ignored": 42}
        )

    assert result.output == "Hello back"
    assert result.prompt_tokens == 3
    assert result.completion_tokens == 2
    assert result.estimated_cost_usd == pytest.approx(0.000014)


async def test_openai_provider_rejects_response_without_text() -> None:
    async def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"output": [], "usage": {}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = OpenAIProvider(api_key="secret", model="model", client=client)
        with pytest.raises(PermanentProviderError, match="did not contain output text"):
            await provider.complete("Hello", metadata={})


@pytest.mark.parametrize(
    ("status_code", "error_type", "retryable", "retry_after_seconds"),
    [
        (400, PermanentProviderError, False, None),
        (429, TransientProviderError, True, 17.0),
        (503, TransientProviderError, True, 17.0),
    ],
)
async def test_openai_provider_classifies_http_failures(
    status_code: int,
    error_type: type[PermanentProviderError] | type[TransientProviderError],
    retryable: bool,
    retry_after_seconds: float | None,
) -> None:
    async def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            headers={"Retry-After": "17"},
            json={"error": {"message": "sensitive detail"}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = OpenAIProvider(api_key="secret", model="model", client=client)
        with pytest.raises(error_type) as captured:
            await provider.complete("Hello", metadata={})

    assert captured.value.retryable is retryable
    assert captured.value.retry_after_seconds == retry_after_seconds
    assert str(status_code) in str(captured.value)
    assert "sensitive detail" not in str(captured.value)


def test_retry_after_parser_accepts_delay_and_http_date() -> None:
    now = datetime(2026, 10, 15, 8, 0, tzinfo=UTC)

    assert _parse_retry_after(" 17 ", now=now) == 17.0
    assert _parse_retry_after(format_datetime(now + timedelta(seconds=30)), now=now) == 30.0
    assert _parse_retry_after(format_datetime(now - timedelta(seconds=1)), now=now) == 0.0
    assert _parse_retry_after("not-a-retry-date", now=now) is None


def test_provider_retry_after_cap_must_be_bounded() -> None:
    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        Settings(app_env="test", provider_retry_after_max_seconds=-1)
    with pytest.raises(ValidationError, match="less than or equal to 3600"):
        Settings(app_env="test", provider_retry_after_max_seconds=3601)


def test_openai_provider_is_registered_only_when_configured() -> None:
    unconfigured = build_provider_registry(Settings(app_env="test", openai_api_key=None))
    with pytest.raises(ValueError, match="Unknown LLM provider"):
        unconfigured.get("openai")
    empty = build_provider_registry(Settings(app_env="test", openai_api_key=""))
    with pytest.raises(ValueError, match="Unknown LLM provider"):
        empty.get("openai")

    configured = build_provider_registry(
        Settings(app_env="test", openai_api_key="test-secret", openai_model="test-model")
    )
    assert isinstance(configured.get("openai"), OpenAIProvider)


def test_provider_cost_rates_must_be_configured_as_a_pair() -> None:
    with pytest.raises(ValueError, match="configured together"):
        Settings(
            app_env="test",
            openai_input_cost_per_million_tokens_usd=1,
        )
    with pytest.raises(ValueError, match="finite and non-negative"):
        OpenAIProvider(
            api_key="secret",
            model="model",
            input_cost_per_million_tokens_usd=-1,
            output_cost_per_million_tokens_usd=1,
        )


async def test_anthropic_provider_uses_messages_api_and_normalizes_result() -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://api.anthropic.test/v1/messages"
        assert request.headers["Authorization"] == "Bearer test-secret"
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert json.loads(request.content) == {
            "model": "test-model",
            "max_tokens": 512,
            "messages": [{"role": "user", "content": "Hello"}],
        }
        return httpx.Response(
            200,
            json={
                "content": [
                    {"type": "thinking", "thinking": "internal"},
                    {"type": "text", "text": "Hello from Claude"},
                ],
                "usage": {"input_tokens": 4, "output_tokens": 3},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = AnthropicProvider(
            api_key="test-secret",
            model="test-model",
            max_tokens=512,
            base_url="https://api.anthropic.test/v1/",
            input_cost_per_million_tokens_usd=3,
            output_cost_per_million_tokens_usd=15,
            client=client,
        )
        result = await provider.complete("Hello", metadata={"run_id": "run-1"})

    assert result.output == "Hello from Claude"
    assert result.prompt_tokens == 4
    assert result.completion_tokens == 3
    assert result.estimated_cost_usd == pytest.approx(0.000057)


async def test_anthropic_provider_rejects_response_without_text() -> None:
    async def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"content": [], "usage": {}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = AnthropicProvider(api_key="secret", model="model", max_tokens=512, client=client)
        with pytest.raises(PermanentProviderError, match="did not contain output text"):
            await provider.complete("Hello", metadata={})


async def test_anthropic_provider_classifies_transport_failures_as_transient() -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sensitive network detail", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = AnthropicProvider(
            api_key="secret",
            model="model",
            max_tokens=512,
            client=client,
        )
        with pytest.raises(TransientProviderError, match="ConnectError") as captured:
            await provider.complete("Hello", metadata={})

    assert captured.value.retryable is True
    assert "sensitive network detail" not in str(captured.value)


def test_anthropic_provider_is_registered_only_when_configured() -> None:
    unconfigured = build_provider_registry(Settings(app_env="test", anthropic_api_key=None))
    with pytest.raises(ValueError, match="Unknown LLM provider"):
        unconfigured.get("anthropic")
    empty = build_provider_registry(Settings(app_env="test", anthropic_api_key=""))
    with pytest.raises(ValueError, match="Unknown LLM provider"):
        empty.get("anthropic")

    configured = build_provider_registry(
        Settings(
            app_env="test",
            anthropic_api_key="test-secret",
            anthropic_model="test-model",
            anthropic_max_tokens=512,
        )
    )
    assert isinstance(configured.get("anthropic"), AnthropicProvider)
