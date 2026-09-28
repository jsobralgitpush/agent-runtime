# ADR 008: Integrate Anthropic through the Messages API

## Status

Accepted

## Context

The runtime can execute against OpenAI, but its provider-neutral workflow contract is more useful
when a second hosted provider implements the same boundary. The v0.3 roadmap explicitly calls for
an Anthropic adapter, while credential persistence remains a separate security decision.

## Decision

Add one `anthropic` adapter using the Anthropic Messages API. Register it only when
`ANTHROPIC_API_KEY` is present, load the credential from process configuration, authenticate with a
Bearer token, send the required stable API-version header, and normalize text blocks and token usage
into `LLMResult`. Make the model, base URL, and output-token ceiling configurable.

Use the existing direct HTTP dependency rather than adding a provider SDK. This keeps the wire
contract explicit and allows deterministic tests with the same injectable client as the OpenAI
adapter.

## Consequences

- Workflows can select `provider: anthropic` or include it in an explicit fallback chain.
- The API key is neither persisted in workflow definitions nor written to runtime telemetry.
- Thinking and other non-text response blocks are ignored by the normalized text interface.
- Cost remains `null`; future pricing telemetry must account for model changes and cache tokens.
- Streaming, tool-use blocks, and encrypted tenant credentials remain separate work.
