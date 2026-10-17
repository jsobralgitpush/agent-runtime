# ADR 025: Bound provider-directed retry delays

## Status

Accepted

## Context

The engine classifies throttling and server failures as transient, but previously retried them using
only workflow-configured exponential backoff. OpenAI, Anthropic, and compatible HTTP services may
return `Retry-After` to communicate a minimum cooldown. Ignoring a valid hint can repeat a request
while the service is still unavailable, wasting quota and increasing load.

The header is controlled by an external service. Accepting an arbitrarily large value would let one
response stall a synchronous request or worker for an operationally unsafe duration. A fallback
chain can also return different hints, while the next step attempt repeats the entire chain.

## Decision

Parse `Retry-After` from transient built-in provider responses in both standard forms: non-negative
delay-seconds and an HTTP date. Carry the normalized number of seconds on `TransientProviderError`
without exposing the raw header or response body.

After a complete fallback chain fails, retain the largest valid hint because the next attempt calls
the full chain again. The retry loop waits for the greater of its exponential backoff and that hint,
then caps only the provider-directed component with `PROVIDER_RETRY_AFTER_MAX_SECONDS`. The setting
defaults to 60 seconds, accepts zero to disable provider-directed extension, and is limited to one
hour. Existing workflow-wide execution deadlines still fail the run rather than exceeding its time
budget. Invalid headers are ignored. Tool retry behavior is unchanged.

## Consequences

Short throttling and maintenance windows are respected without weakening workflow backoff. Mixed
fallback chains use one conservative cooldown, so a hint from one provider can delay retrying
another provider. Operators can tune or disable that extension independently of workflow
definitions.

Hints above the configured cap are deliberately shortened and may cause another request before the
provider's requested time. The cap protects bounded execution; deployments that prefer provider
policy over latency can raise it within the one-hour limit. ADR 026 adds independent provider-call
timeouts, and ADR 027 adds step-local permanent-failure memory. Circuit breakers and provider-aware
remote cancellation remain future work.
