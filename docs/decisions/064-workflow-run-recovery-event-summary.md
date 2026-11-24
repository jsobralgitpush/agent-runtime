# ADR 064: Summarize workflow run recovery events

## Context

Recovery history can be filtered by outcome and creation time, but an operator who only needs
requeue and failure totals must still transfer and count every matching event. That adds avoidable
database materialization, response payload, and client logic for a common operational question.

The remaining explicit roadmap item, encrypted tenant credentials, remains blocked by undefined
tenancy, authentication, authorization, credential ownership, and key-rotation contracts. A
bounded summary extends the existing recovery API without inventing those security contracts.

## Decision

Add `GET /v1/runs/{run_id}/recovery-events/summary`. Return the run ID, a total, and explicit
`requeued` and `failed` counts. Accept the same optional `created_from` inclusive and
`created_before` exclusive bounds as recovery history, including its timezone-awareness and
validation rules.

Use one database query grouped by outcome rather than materializing event rows. Fill missing
outcome groups with zero so the response shape is stable for empty and single-outcome windows.
Reuse the existing run, outcome, and creation-time indexes; no schema migration is required.

## Consequences

- Operators can retrieve compact recovery totals without downloading event history.
- Time-bounded totals use the same half-open window semantics as the event list.
- The summary reflects committed rows at query time and is not a snapshot shared with later calls.
- Both outcome keys are always present, including when their count is zero.
- The endpoint is operational aggregation, not authorization, audit attribution, or retention.
