# ADR 007: Integrate OpenAI through the Responses API

## Status

Accepted

## Context

The runtime has an LLM provider protocol and deterministic fake implementation, but cannot execute
a workflow against a real hosted model. The v0.3 roadmap calls for OpenAI and Anthropic adapters.
Adding both at once would make provider-specific behavior harder to review and test.

## Decision

Add one `openai` adapter using the OpenAI Responses API. Register it only when `OPENAI_API_KEY` is
present, read the credential from the process environment, disable response storage, attach run and
step identifiers as request metadata, and normalize output text and token usage into `LLMResult`.
Use direct HTTP rather than a provider SDK so the protocol boundary and wire behavior remain small
and explicit.

## Consequences

- Workflows can select `provider: openai` without changing engine behavior.
- The API key is neither stored in workflow definitions nor written to runtime telemetry.
- The HTTP client is injectable, allowing deterministic tests without credentials or network calls.
- Cost remains `null` because price tables vary by model and time; a future pricing policy can
  calculate cost from persisted model and token data.
- Anthropic support, credential encryption, streaming, and structured output remain separate work.
