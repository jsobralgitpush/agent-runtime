# ADR 063: Bound workflow run recovery events by creation time

## Context

Recovery history supports outcome filtering and bounded cursor pages, but operators investigating a
known incident window must still traverse every earlier worker recovery. Offset-naive timestamps
are ambiguous across deployments, and inclusive upper bounds make adjacent windows overlap.

The remaining explicit roadmap item, encrypted tenant credentials, remains blocked by undefined
tenancy, authentication, authorization, credential ownership, and key-rotation contracts. Temporal
history queries improve the existing operational API without inventing those security contracts.

## Decision

Add optional `created_from` and `created_before` parameters to
`GET /v1/runs/{run_id}/recovery-events`. Define a half-open interval: `created_from` is inclusive
and `created_before` is exclusive. Require RFC 3339 timezone-aware values, normalize them to UTC,
and reject empty or reversed ranges with the stable
`422 invalid_run_recovery_event_time_range` response.

Apply time predicates before keyset traversal and allow them to compose with the outcome filter.
Normalize SQLite comparisons with `julianday` because stored and bound timestamps can use different
textual precision; PostgreSQL retains native datetime comparisons. Reuse the existing
`(run_id, created_at, id)` and `(run_id, outcome, created_at, id)` indexes, so no migration is
required.

## Consequences

- Operators can inspect only recovery decisions in an incident or reporting window.
- Adjacent half-open windows neither overlap nor leave a boundary gap.
- Equivalent timezone offsets produce the same UTC query.
- Existing clients remain backward compatible when both bounds are omitted.
- Clients must repeat the same bounds and filters while following a cursor.
- Time filtering is not deletion, retention, authorization, or an immutable reporting snapshot.
