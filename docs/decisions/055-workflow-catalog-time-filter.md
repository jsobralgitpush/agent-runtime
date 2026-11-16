# ADR 055: Bound the workflow catalog by creation time

## Context

The workflow catalog supports exact-name and archive-state filters plus keyset pagination, but an
operator investigating a known deployment or incident window must still traverse every newer and
older definition. Offset-naive timestamps are ambiguous across deployments, while inclusive upper
bounds make adjacent reporting windows overlap.

The remaining explicit roadmap item, encrypted tenant credentials, remains blocked by undefined
tenancy, authentication, authorization, credential ownership, and key-rotation contracts. Temporal
catalog queries improve the current single-tenant API without inventing those security contracts.

## Decision

Add optional `created_from` and `created_before` parameters to `GET /v1/workflows`. Define a
half-open interval: `created_from` is inclusive and `created_before` is exclusive. Require RFC 3339
timezone-aware values, normalize them to UTC, and reject empty or reversed ranges with the stable
`422 invalid_workflow_time_range` response.

Apply time predicates before keyset traversal and allow them to compose with exact-name,
archive-state, limit, and cursor filters. Normalize SQLite comparisons with `julianday` because
stored and bound timestamps can use different textual precision; PostgreSQL retains native datetime
comparisons. Reuse the existing `(created_at, id)` index, so no migration is required.

## Consequences

- Operators can inspect definitions created only during an incident or reporting window.
- Adjacent half-open windows neither overlap nor leave a boundary gap.
- Equivalent timezone offsets select the same UTC interval.
- Existing clients remain backward compatible when both bounds are omitted.
- Clients must repeat the same bounds while following a cursor to preserve one logical traversal.
- Time filtering is not deletion or retention and does not limit stored workflow definitions.

The same interval contract is applied to catalog summaries in
[ADR 056](056-time-bounded-workflow-catalog-summary.md).
