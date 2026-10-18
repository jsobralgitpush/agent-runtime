# ADR 028: Allow provider-specific timeout overrides

## Status

Accepted

## Context

ADR 026 adds one optional call timeout shared by every provider in an LLM step. Real fallback chains
are often heterogeneous: a low-latency primary may need a strict limit while a slower reasoning
model or regional fallback needs more time. Using the larger value for the whole chain delays
failover; using the smaller value can make a healthy fallback unusable.

Provider names and fallback order already live in the workflow definition, so latency policy should
remain explicit and reviewable there rather than being hidden inside adapters.

## Decision

Add `provider_timeout_overrides`, a map from configured provider name to timeout seconds. Limit the
map to six entries, matching the maximum chain size. Require every key to belong to the step's
primary/fallback chain and every value to be finite, greater than zero, and no larger than 300
seconds. Tool and approval steps reject the field.

For each provider call, prefer its matching override, then fall back to
`provider_timeout_seconds`. If neither exists, retain the original shared step-timeout behavior.
Provider-specific values do not extend the per-step attempt timeout or workflow deadline. Preserve
the same transient timeout classification, fallback, retry, and local cancellation semantics from
ADR 026.

## Consequences

Workflows can fail over quickly from latency-sensitive providers while granting slower fallbacks a
realistic response window. Validation prevents misspelled, undeclared, or unbounded override policy.
Existing definitions remain compatible and provider adapters remain unaware of orchestration
timeouts.

The configuration is intentionally one value per provider name within a step; it does not expose
HTTP connect/read/write phases or dynamic percentile-based budgets. Operators must ensure the outer
step timeout can accommodate the intended sequence. Local cancellation may still race with remote
execution and billing.
