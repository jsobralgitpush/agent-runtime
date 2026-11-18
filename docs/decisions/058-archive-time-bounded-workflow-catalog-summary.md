# ADR 058: Bound workflow catalog summaries by archive time

## Context

The workflow catalog can list definitions archived during a known interval, but its compact
archive-state summary can only be bounded by workflow creation time. Operators investigating a
cleanup, incident, or restoration window must therefore load complete workflow definitions just to
count the affected archived rows.

The remaining explicit roadmap item, encrypted tenant credentials, remains blocked by undefined
tenancy, authentication, authorization, credential ownership, and key-rotation contracts. Extending
the existing catalog aggregate closes a focused lifecycle-query gap without inventing those security
contracts.

## Decision

Add optional `archived_from` and `archived_before` parameters to
`GET /v1/workflows/summary`. Reuse the catalog's timezone-aware, half-open interval: the lower bound
is inclusive and the upper bound is exclusive. Normalize values to UTC and reject naive, empty, or
reversed ranges with the stable `422 invalid_workflow_archive_time_range` response.

Apply archive-time predicates before the grouped count and allow them to compose with exact name and
creation-time bounds. Because active workflows have a null `archived_at`, either archive bound
naturally excludes them; the response still returns an explicit active count of zero. Normalize
SQLite comparisons with `julianday`, retain native PostgreSQL datetime comparisons, and reuse the
existing `archived_at` index. No migration is required.

## Consequences

- Operators can count workflows archived during a known interval without loading definitions.
- Adjacent half-open windows neither overlap nor leave a boundary gap.
- Equivalent timezone offsets select the same UTC interval.
- Existing clients remain backward compatible when both bounds are omitted.
- Archive-bounded summaries cannot include active workflows.
- Counts reflect committed rows at query time and are not immutable reporting snapshots.
- Archive-time filtering is not deletion, retention, or tenant authorization.
