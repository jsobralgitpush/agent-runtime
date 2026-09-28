import json

import httpx
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.providers import (
    AnthropicProvider,
    FakeLLMProvider,
    OpenAIProvider,
    ProviderRegistry,
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
            client=client,
        )
        result = await provider.complete(
            "Hello", metadata={"run_id": "run-1", "step": "draft", "ignored": 42}
        )

    assert result.output == "Hello back"
    assert result.prompt_tokens == 3
    assert result.completion_tokens == 2
    assert result.estimated_cost_usd is None


async def test_openai_provider_rejects_response_without_text() -> None:
    async def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"output": [], "usage": {}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = OpenAIProvider(api_key="secret", model="model", client=client)
        with pytest.raises(ValueError, match="did not contain output text"):
            await provider.complete("Hello", metadata={})


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
            client=client,
        )
        result = await provider.complete("Hello", metadata={"run_id": "run-1"})

    assert result.output == "Hello from Claude"
    assert result.prompt_tokens == 4
    assert result.completion_tokens == 3
    assert result.estimated_cost_usd is None


async def test_anthropic_provider_rejects_response_without_text() -> None:
    async def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"content": [], "usage": {}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = AnthropicProvider(api_key="secret", model="model", max_tokens=512, client=client)
        with pytest.raises(ValueError, match="did not contain output text"):
            await provider.complete("Hello", metadata={})


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
