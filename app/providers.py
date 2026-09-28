import hashlib
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, Field

from app.config import Settings


@dataclass(frozen=True)
class LLMResult:
    output: str
    prompt_tokens: int
    completion_tokens: int
    estimated_cost_usd: float | None
    provider: str | None = None


class LLMProvider(Protocol):
    async def complete(self, prompt: str, *, metadata: dict[str, Any]) -> LLMResult: ...


class FakeLLMProvider:
    """Deterministic provider for local development, demos, and tests."""

    async def complete(self, prompt: str, *, metadata: dict[str, Any]) -> LLMResult:
        digest = hashlib.sha256(prompt.encode()).hexdigest()[:8]
        output = f"[fake:{digest}] {prompt}"
        prompt_tokens = max(1, len(prompt.split()))
        completion_tokens = max(1, len(output.split()))
        return LLMResult(
            output=output,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            estimated_cost_usd=0.0,
        )


class _OpenAIContent(BaseModel):
    type: str
    text: str | None = None


class _OpenAIOutputItem(BaseModel):
    content: list[_OpenAIContent] = Field(default_factory=list)


class _OpenAIUsage(BaseModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)


class _OpenAIResponse(BaseModel):
    output: list[_OpenAIOutputItem] = Field(default_factory=list)
    usage: _OpenAIUsage = Field(default_factory=_OpenAIUsage)


class OpenAIProvider:
    """OpenAI Responses API adapter with an injectable HTTP client for tests."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key or not model:
            raise ValueError("OpenAI API key and model must not be empty")
        self._api_key = api_key
        self._model = model
        self._url = f"{base_url.rstrip('/')}/responses"
        self._client = client

    async def complete(self, prompt: str, *, metadata: dict[str, Any]) -> LLMResult:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        run_id = metadata.get("run_id")
        step_key = metadata.get("step")
        if isinstance(run_id, str) and isinstance(step_key, str):
            headers["X-Client-Request-Id"] = f"{run_id}:{step_key}"
        payload = {
            "model": self._model,
            "input": prompt,
            "store": False,
            "metadata": {key: value for key, value in metadata.items() if isinstance(value, str)},
        }

        if self._client is None:
            async with httpx.AsyncClient(timeout=None) as client:
                response = await client.post(self._url, headers=headers, json=payload)
        else:
            response = await self._client.post(self._url, headers=headers, json=payload)
        response.raise_for_status()

        parsed = _OpenAIResponse.model_validate(response.json())
        output = "\n".join(
            content.text
            for item in parsed.output
            for content in item.content
            if content.type == "output_text" and content.text
        )
        if not output:
            raise ValueError("OpenAI response did not contain output text")
        return LLMResult(
            output=output,
            prompt_tokens=parsed.usage.input_tokens,
            completion_tokens=parsed.usage.output_tokens,
            estimated_cost_usd=None,
        )


class _AnthropicContent(BaseModel):
    type: str
    text: str | None = None


class _AnthropicUsage(BaseModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)


class _AnthropicResponse(BaseModel):
    content: list[_AnthropicContent] = Field(default_factory=list)
    usage: _AnthropicUsage = Field(default_factory=_AnthropicUsage)


class AnthropicProvider:
    """Anthropic Messages API adapter with an injectable HTTP client for tests."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        max_tokens: int,
        base_url: str = "https://api.anthropic.com/v1",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key or not model:
            raise ValueError("Anthropic API key and model must not be empty")
        if max_tokens < 1:
            raise ValueError("Anthropic max tokens must be positive")
        self._api_key = api_key
        self._model = model
        self._max_tokens = max_tokens
        self._url = f"{base_url.rstrip('/')}/messages"
        self._client = client

    async def complete(self, prompt: str, *, metadata: dict[str, Any]) -> LLMResult:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "anthropic-version": "2023-06-01",
        }
        payload = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }

        if self._client is None:
            async with httpx.AsyncClient(timeout=None) as client:
                response = await client.post(self._url, headers=headers, json=payload)
        else:
            response = await self._client.post(self._url, headers=headers, json=payload)
        response.raise_for_status()

        parsed = _AnthropicResponse.model_validate(response.json())
        output = "\n".join(
            content.text for content in parsed.content if content.type == "text" and content.text
        )
        if not output:
            raise ValueError("Anthropic response did not contain output text")
        return LLMResult(
            output=output,
            prompt_tokens=parsed.usage.input_tokens,
            completion_tokens=parsed.usage.output_tokens,
            estimated_cost_usd=None,
        )


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, LLMProvider] = {"fake": FakeLLMProvider()}

    def get(self, name: str) -> LLMProvider:
        try:
            return self._providers[name]
        except KeyError as exc:
            raise ValueError(f"Unknown LLM provider: {name}") from exc

    def register(self, name: str, provider: LLMProvider) -> None:
        if not name or name in self._providers:
            raise ValueError(f"Provider already registered or invalid: {name}")
        self._providers[name] = provider


def build_provider_registry(settings: Settings) -> ProviderRegistry:
    registry = ProviderRegistry()
    api_key = (
        settings.openai_api_key.get_secret_value() if settings.openai_api_key is not None else ""
    )
    if api_key:
        registry.register(
            "openai",
            OpenAIProvider(
                api_key=api_key,
                model=settings.openai_model,
                base_url=settings.openai_base_url,
            ),
        )
    anthropic_api_key = (
        settings.anthropic_api_key.get_secret_value()
        if settings.anthropic_api_key is not None
        else ""
    )
    if anthropic_api_key:
        registry.register(
            "anthropic",
            AnthropicProvider(
                api_key=anthropic_api_key,
                model=settings.anthropic_model,
                max_tokens=settings.anthropic_max_tokens,
                base_url=settings.anthropic_base_url,
            ),
        )
    return registry
