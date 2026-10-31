# ADR 041: Completion-bounded workflow run summaries

## Context

The workflow run summary can count current statuses across all runs or runs created in a time
window. Creation time does not answer throughput questions such as how many runs completed or failed
during an incident, release, or reporting period. Clients would otherwise need to download and
aggregate detailed history.

## Decision

Accept optional `completed_from` and `completed_before` parameters on
`GET /v1/workflows/{workflow_id}/runs/summary`. Reuse the detailed history contract: a half-open
`[completed_from, completed_before)` interval, required RFC 3339 timezone offsets, UTC
normalization, and deterministic `422 invalid_run_time_range` errors for naive or empty/reversed
ranges.

Apply completion predicates before `GROUP BY status`. Comparisons against nullable `completed_at`
exclude pending, running, and waiting runs whenever either completion bound is present. Allow
creation and completion windows together; their predicates form an intersection. Reuse the existing
`(workflow_id, completed_at)` index and keep explicit zeroes for states absent from the result.

## Consequences

- Operators can request compact completed-versus-failed throughput for a defined period.
- Boundary and timezone behavior matches detailed history and creation-bounded summaries.
- No schema migration is required.
- The response reports each matching run's current terminal status; it is not an immutable status
  transition ledger.
- Later data correction can change the same historical aggregate.
- Completion filtering does not replace tenant authorization, retention, archival, or audit events.
