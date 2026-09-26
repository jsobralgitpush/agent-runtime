# ADR 006: Keep provider fallback explicit and ordered

## Status

Accepted

## Context

Provider outages and rate limits should not necessarily fail an otherwise portable LLM step. A
fallback policy hidden in provider adapters would make execution order and cost difficult to inspect.

## Decision

Allow an LLM step to declare up to five ordered fallback provider names after its primary provider.
The engine tries each provider in order within the step's existing timeout and retry boundary. The
successful provider name is persisted on the step run. If all providers fail, the step records a
stable aggregate error containing provider names but not their exception messages.

## Consequences

- Workflow definitions make fallback order reviewable and deterministic.
- Step telemetry shows which provider produced the persisted output.
- Provider exception messages are omitted from aggregate errors and structured failure logs.
- A provider that consumes the entire step timeout prevents later fallbacks; per-provider timeout
  allocation remains a future policy decision.
