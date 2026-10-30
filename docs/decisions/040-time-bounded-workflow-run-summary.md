# ADR 040: Time-bounded workflow run summaries

## Context

The workflow run summary provides bounded status counts, but it currently aggregates the complete
history. Daily reports, incident windows, and deployment comparisons need counts for a defined
creation period. Without summary bounds, clients must download detailed history pages and aggregate
them locally.

## Decision

Accept optional `created_from` and `created_before` parameters on
`GET /v1/workflows/{workflow_id}/runs/summary`. Reuse the detailed history contract: a half-open
`[created_from, created_before)` interval, required RFC 3339 timezone offsets, UTC normalization,
and deterministic `422 invalid_run_time_range` errors for naive or empty/reversed ranges.

Apply the workflow and time predicates before the existing `GROUP BY status`. Reuse the
`(workflow_id, status, created_at, id)` index and keep the response shape unchanged, including
explicit zeroes for states absent from the interval.

## Consequences

- Operators can request compact lifecycle distributions for repeatable time windows.
- Boundary behavior matches detailed run history, avoiding a second timestamp contract.
- No schema migration is required.
- Counts reflect run creation time, not completion time or the status held during the interval.
- The aggregate is evaluated at query time; later status transitions can change results for the
  same creation window.
- Time filtering does not replace tenant authorization, retention, archival, or immutable reports.
