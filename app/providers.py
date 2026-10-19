import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from email.utils import parsedate_to_datetime
from math import isfinite
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


class ProviderError(RuntimeError):
    """Sanitized provider failure with an explicit retry classification."""

    retryable: bool

    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class TransientProviderError(ProviderError):
    retryable = True


class ProviderTimeoutError(TransientProviderError):
    """A provider call exceeded its independent deadline."""


class PermanentProviderError(ProviderError):
    retryable = False


_TRANSIENT_HTTP_STATUS_CODES = frozenset({408, 409, 425, 429})


def _parse_retry_after(
    value: str | None,
    *,
    now: datetime | None = None,
) -> float | None:
    if value is None:
        return None
    normalized = value.strip()
    if normalized.isdigit():
        return float(normalized)
    try:
        retry_at = parsedate_to_datetime(normalized)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        observed_at = now or datetime.now(UTC)
        return max(0.0, (retry_at.astimezone(UTC) - observed_at.astimezone(UTC)).total_seconds())
    except (OverflowError, TypeError, ValueError):
        return None


def _raise_for_provider_status(response: httpx.Response, provider: str) -> None:
    if response.is_success:
        return
    error_type = (
        TransientProviderError
        if response.status_code in _TRANSIENT_HTTP_STATUS_CODES or response.status_code >= 500
        else PermanentProviderError
    )
    retry_after_seconds = (
        _parse_retry_after(response.headers.get("Retry-After"))
        if error_type is TransientProviderError
        else None
    )
    raise error_type(
        f"{provider} request failed with HTTP status {response.status_code}",
        retry_after_seconds=retry_after_seconds,
    )


def _validate_cost_rates(
    input_rate: float | None, output_rate: float | None
) -> tuple[float | None, float | None]:
    if (input_rate is None) != (output_rate is None):
        raise ValueError("Input and output cost rates must be configured together")
    if input_rate is not None and (
        input_rate < 0
        or output_rate is None
        or output_rate < 0
        or not isfinite(input_rate)
        or not isfinite(output_rate)
    ):
        raise ValueError("Cost rates must be finite and non-negative")
    return input_rate, output_rate


def _estimate_cost_usd(
    prompt_tokens: int,
    completion_tokens: int,
    input_rate: float | None,
    output_rate: float | None,
) -> float | None:
    if input_rate is None or output_rate is None:
        return None
    cost = (
        Decimal(prompt_tokens) * Decimal(str(input_rate))
        + Decimal(completion_tokens) * Decimal(str(output_rate))
    ) / Decimal(1_000_000)
    return float(cost)


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
        input_cost_per_million_tokens_usd: float | None = None,
        output_cost_per_million_tokens_usd: float | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key or not model:
            raise ValueError("OpenAI API key and model must not be empty")
        self._api_key = api_key
        self._model = model
        self._url = f"{base_url.rstrip('/')}/responses"
        self._input_cost_rate, self._output_cost_rate = _validate_cost_rates(
            input_cost_per_million_tokens_usd, output_cost_per_million_tokens_usd
        )
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

        try:
            if self._client is None:
                async with httpx.AsyncClient(timeout=None) as client:
                    response = await client.post(self._url, headers=headers, json=payload)
            else:
                response = await self._client.post(self._url, headers=headers, json=payload)
        except httpx.RequestError as exc:
            raise TransientProviderError(
                f"OpenAI request failed with {type(exc).__name__}"
            ) from exc
        _raise_for_provider_status(response, "OpenAI")

        try:
            parsed = _OpenAIResponse.model_validate(response.json())
        except ValueError as exc:
            raise PermanentProviderError("OpenAI returned an invalid response") from exc
        output = "\n".join(
            content.text
            for item in parsed.output
            for content in item.content
            if content.type == "output_text" and content.text
        )
        if not output:
            raise PermanentProviderError("OpenAI response did not contain output text")
        return LLMResult(
            output=output,
            prompt_tokens=parsed.usage.input_tokens,
            completion_tokens=parsed.usage.output_tokens,
            estimated_cost_usd=_estimate_cost_usd(
                parsed.usage.input_tokens,
                parsed.usage.output_tokens,
                self._input_cost_rate,
                self._output_cost_rate,
            ),
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
        input_cost_per_million_tokens_usd: float | None = None,
        output_cost_per_million_tokens_usd: float | None = None,
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
        self._input_cost_rate, self._output_cost_rate = _validate_cost_rates(
            input_cost_per_million_tokens_usd, output_cost_per_million_tokens_usd
        )
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

        try:
            if self._client is None:
                async with httpx.AsyncClient(timeout=None) as client:
                    response = await client.post(self._url, headers=headers, json=payload)
            else:
                response = await self._client.post(self._url, headers=headers, json=payload)
        except httpx.RequestError as exc:
            raise TransientProviderError(
                f"Anthropic request failed with {type(exc).__name__}"
            ) from exc
        _raise_for_provider_status(response, "Anthropic")

        try:
            parsed = _AnthropicResponse.model_validate(response.json())
        except ValueError as exc:
            raise PermanentProviderError("Anthropic returned an invalid response") from exc
        output = "\n".join(
            content.text for content in parsed.content if content.type == "text" and content.text
        )
        if not output:
            raise PermanentProviderError("Anthropic response did not contain output text")
        return LLMResult(
            output=output,
            prompt_tokens=parsed.usage.input_tokens,
            completion_tokens=parsed.usage.output_tokens,
            estimated_cost_usd=_estimate_cost_usd(
                parsed.usage.input_tokens,
                parsed.usage.output_tokens,
                self._input_cost_rate,
                self._output_cost_rate,
            ),
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

    def names(self) -> tuple[str, ...]:
        """Return configured provider names without exposing adapters or credentials."""
        return tuple(sorted(self._providers))


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
                input_cost_per_million_tokens_usd=(
                    settings.openai_input_cost_per_million_tokens_usd
                ),
                output_cost_per_million_tokens_usd=(
                    settings.openai_output_cost_per_million_tokens_usd
                ),
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
                input_cost_per_million_tokens_usd=(
                    settings.anthropic_input_cost_per_million_tokens_usd
                ),
                output_cost_per_million_tokens_usd=(
                    settings.anthropic_output_cost_per_million_tokens_usd
                ),
            ),
        )
    return registry
