# ADR 052: Bound workflow lifecycle events by creation time

## Context

Lifecycle history supports action filtering and bounded cursor pages, but operators investigating a
known incident window must still traverse every earlier transition. Offset-naive timestamps are
ambiguous across deployments, and inclusive upper bounds make adjacent windows overlap.

The remaining explicit roadmap item, encrypted tenant credentials, remains blocked by undefined
tenancy, authentication, authorization, credential ownership, and key-rotation contracts. Temporal
history queries improve the existing lifecycle API without inventing those security contracts.

## Decision

Add optional `created_from` and `created_before` parameters to
`GET /v1/workflows/{workflow_id}/lifecycle-events`. Define a half-open interval:
`created_from` is inclusive and `created_before` is exclusive. Require RFC 3339 timezone-aware
values, normalize them to UTC, and reject empty or reversed ranges with the stable
`422 invalid_workflow_lifecycle_event_time_range` response.

Apply time predicates before keyset traversal and allow them to compose with the action filter.
Normalize SQLite comparisons with `julianday` because stored and bound timestamps can use different
textual precision; PostgreSQL retains native datetime comparisons. Reuse the existing
`(workflow_id, created_at, id)` and `(workflow_id, action, created_at, id)` indexes, so no migration
is required.

## Consequences

- Operators can inspect only transitions in an incident or reporting window.
- Adjacent half-open windows neither overlap nor leave a boundary gap.
- Equivalent timezone offsets produce the same UTC query.
- Existing clients remain backward compatible when both bounds are omitted.
- Clients must repeat the same bounds while following a cursor to preserve one logical traversal.
- Time filtering is not deletion or retention and does not limit stored lifecycle history.
