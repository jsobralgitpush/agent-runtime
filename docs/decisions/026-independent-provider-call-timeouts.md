# ADR 026: Bound provider calls independently

## Status

Accepted

## Context

The step timeout bounds an entire attempt, including every provider in an ordered fallback chain.
A slow primary provider can therefore consume that whole interval and prevent otherwise healthy
fallbacks from running. Lowering the step timeout does not solve the problem because it shortens the
chain's total opportunity rather than bounding one call.

Provider adapters deliberately use no independent HTTP client timeout because the engine owns
workflow execution policy. The runtime needs an explicit, inspectable limit that also works for
custom providers and remains subordinate to existing step and workflow deadlines.

## Decision

Allow LLM steps to set an optional `provider_timeout_seconds` value greater than zero and no larger
than 300 seconds. Apply the same limit independently to each provider call in the chain. Tool and
approval steps reject the field. At acceptance, one value applied to the complete chain; ADR 028
adds validated provider-specific overrides while retaining this value as the default.

Wrap each provider completion coroutine in a local wall-clock timeout. Convert expiration into a
sanitized `ProviderTimeoutError`, classified as transient, then continue normal fallback traversal.
If the full chain fails, existing retry policy may repeat it. The enclosing step-attempt timeout and
workflow deadline remain hard outer bounds and are never extended by this field. Omitting the field
preserves the existing shared-timeout behavior.

## Consequences

A stalled provider no longer has to prevent later fallbacks, and workflow definitions make the
latency policy reviewable. The same mechanism applies to built-in and custom providers without
coupling the engine to HTTP-client-specific connect, read, write, or pool timeout settings.

Local cancellation cannot prove that a remote request stopped before execution. A timed-out call
may still complete or incur cost at the provider, and a fallback may add more cost. The setting is
therefore opt-in and should be chosen with provider latency, billing, and the outer step deadline in
mind. Circuit breakers and provider-aware cancellation protocols remain separate work.
