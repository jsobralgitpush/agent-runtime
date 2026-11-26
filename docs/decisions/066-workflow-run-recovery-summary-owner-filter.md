# ADR 066: Filter recovery summaries by expired worker

## Context

Recovery event listing can isolate one expired worker, but its compact summary can only aggregate
the whole run or a time window. An operator comparing one worker's requeue and failure decisions
must therefore download the matching events and count them locally.

The remaining explicit roadmap item, encrypted tenant credentials, remains blocked by undefined
tenancy, authentication, authorization, credential ownership, and key-rotation contracts. A
worker-scoped summary closes a concrete operational gap without inventing those contracts.

## Decision

Add the optional `expired_lease_owner` query parameter to
`GET /v1/runs/{run_id}/recovery-events/summary`. Reuse the list endpoint's exact, case-sensitive
matching and length validation. Apply the owner predicate together with the optional time bounds
before grouping by outcome.

Reuse `(run_id, expired_lease_owner, created_at, id)` from migration `0017`; no schema change is
required. Preserve explicit `requeued` and `failed` zeroes when no event matches.

## Consequences

- Operators can retrieve one worker's outcome totals without materializing event rows.
- Summary and list worker filters use the same exact-match contract.
- The response remains stable for unknown, case-different, and empty result sets.
- Worker identifiers remain operational labels, not trusted attribution or authorization.
- The summary reflects committed rows at query time rather than a reporting snapshot.
