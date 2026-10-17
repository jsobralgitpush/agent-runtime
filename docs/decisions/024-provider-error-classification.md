# ADR 024: Classify provider failures before retrying

## Status

Accepted

## Context

The engine previously retried every failed LLM provider chain whenever the workflow requested more
than one attempt. Retrying authentication errors, invalid requests, or malformed successful
responses wastes time and quota without making success more likely. Raw `httpx` exceptions also
expose inconsistent types and may include response or network details that should not enter durable
run errors.

Fallback and retry solve different problems. A permanent failure from one provider does not mean a
configured alternative will fail, while repeating a chain whose every failure is permanent is not
useful.

## Decision

Define sanitized `TransientProviderError` and `PermanentProviderError` classes. The OpenAI and
Anthropic adapters classify request transport failures, HTTP `408`, `409`, `425`, `429`, and `5xx`
responses as transient. Other non-success HTTP responses and invalid or textless successful
responses are permanent. Messages include only the provider plus HTTP status or transport exception
class; provider response bodies and transport messages are excluded.

Always traverse the configured fallback chain. If every provider fails, propagate whether any
failure was transient to the step retry loop. Stop after the current attempt when the whole chain
was permanent; otherwise preserve configured exponential retry behavior. Do not change tool retry
semantics. Treat unknown exceptions from custom providers as retryable for backward compatibility.

## Consequences

Invalid credentials and requests fail after one provider-chain attempt even when a workflow allows
more retries. Throttling, server errors, and transport failures can still recover through fallback
or configured step retries. At acceptance time, a mixed permanent/transient chain called permanent
failures again on every retry; ADR 027 adds step-local memory to avoid that repeated work.

At acceptance time, this policy did not honor `Retry-After` or independent provider timeouts; ADR
025 and ADR 026 add bounded support respectively. Circuit breakers and provider-aware remote
cancellation remain separate work. Custom providers gain the same behavior when they raise the
normalized classes explicitly.
