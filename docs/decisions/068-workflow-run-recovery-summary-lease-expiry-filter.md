# ADR 068: Filter recovery summaries by lease expiry

## Context

Recovery event listing can select leases that expired during an incident window, but its compact
summary can only aggregate by recovery time. Operators who need outcome totals for the original
lease-deadline window must therefore download matching events and count them locally.

The remaining explicit roadmap item, encrypted tenant credentials, remains blocked by undefined
tenancy, authentication, authorization, credential ownership, and key-rotation contracts. A
lease-expiry-bounded summary closes a concrete operational gap without inventing those contracts.

## Decision

Add optional `lease_expired_from` and `lease_expired_before` parameters to
`GET /v1/runs/{run_id}/recovery-events/summary`. Reuse the list endpoint's timezone-aware,
half-open interval, UTC normalization, and stable validation errors. Apply lease-expiry predicates
together with the exact expired-owner and recovery-time predicates before grouping by outcome.

Reuse `(run_id, lease_expired_at, created_at, id)` from migration `0018`; no schema change is
required. Preserve explicit `requeued` and `failed` zeroes when no event matches.

## Consequences

- Operators can retrieve outcome totals for the actual lease-deadline incident window.
- Summary and list endpoints share the same lease-expiry interval contract.
- Lease-expiry, recovery-time, and expired-owner selectors compose.
- The response remains stable when a filtered interval is empty.
- Clock skew in stored lease deadlines can affect interval membership.
- Aggregates reflect committed rows at query time rather than a reporting snapshot.
