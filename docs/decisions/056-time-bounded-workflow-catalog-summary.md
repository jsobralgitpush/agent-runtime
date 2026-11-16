# ADR 056: Bound workflow catalog summaries by creation time

## Context

The workflow catalog can now be traversed within a creation-time window, but its archive-state
summary remains catalog-wide. An operator who needs state totals for an incident or deployment
window must load the filtered definitions and count them locally, defeating the summary endpoint's
purpose.

The remaining explicit roadmap item, encrypted tenant credentials, remains blocked by undefined
tenancy, authentication, authorization, credential ownership, and key-rotation contracts. A
time-bounded summary completes the existing catalog query contract without inventing those security
contracts.

## Decision

Accept optional `created_from` and `created_before` parameters on `GET /v1/workflows/summary`.
Reuse the catalog's timezone-aware, half-open `[created_from, created_before)` interval, UTC
normalization, and stable `422 invalid_workflow_time_range` errors. Allow the bounds to compose with
the existing exact-name filter.

Apply the time predicates before grouping by archive state. Normalize SQLite comparisons with
`julianday`; PostgreSQL retains native datetime comparisons. Reuse the existing creation-time and
name indexes, return explicit zeroes for absent groups, and require no migration.

## Consequences

- Operators can retrieve compact active and archived totals for a reporting window.
- List and summary endpoints use identical creation-time boundaries and validation.
- Empty windows preserve the stable response shape with both counts set to zero.
- Counts reflect committed rows at query time and are not a snapshot shared with later requests.
- Time-bounded aggregation is not tenant authorization, deletion, or retention policy.
