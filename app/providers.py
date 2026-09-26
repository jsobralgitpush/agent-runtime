import hashlib
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class LLMResult:
    output: str
    prompt_tokens: int
    completion_tokens: int
    estimated_cost_usd: float
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
