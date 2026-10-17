# ADR 027: Remember permanent provider failures during step retries

## Status

Accepted

## Context

The engine retries a failed provider chain when at least one provider reports a transient failure.
If the same chain also contains a permanent failure, such as invalid credentials or a rejected
request, every step attempt currently calls that provider again. The repeated call cannot recover,
adds latency and noise, and may consume provider quota before reaching a viable fallback.

A process-wide circuit breaker would require shared lifecycle, concurrency, probing, and deployment
semantics. The API and worker currently construct an engine for each run, so introducing durable or
global health state as a side effect of retry optimization would exceed this change's scope.

## Decision

Track provider names that return a normalized permanent failure while one step is actively
executing. When a mixed chain is retried, skip those providers and call only candidates that have
not failed permanently. Continue treating unknown custom-provider exceptions as transient for
backward compatibility. Keep the first attempt's ordered fallback traversal unchanged.

The memory belongs to the retry loop for a single step. It is not shared between steps or runs and
is not persisted. A new execution or replay after process loss starts with the complete configured
chain. Emit a structured `llm_provider_skipped` log with bounded reason
`prior_permanent_failure`; do not include exception messages or payloads.

## Consequences

Mixed failure chains stop repeating known-bad authentication, validation, and schema calls while
transient providers retain their configured retry opportunities. Purely permanent chains still stop
after one attempt, and purely transient chains behave as before.

The state cannot suppress a failing provider across runs or worker processes and is intentionally
reset after recovery. Operators that need fleet-wide suppression, half-open probes, or health
sharing still need a separately designed circuit breaker. A provider incorrectly classifying a
recoverable condition as permanent will be skipped for the rest of that step execution.
