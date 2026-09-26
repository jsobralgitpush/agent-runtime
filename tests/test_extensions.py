import pytest
from pydantic import ValidationError

from app.providers import FakeLLMProvider, ProviderRegistry
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
