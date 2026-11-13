# ADR 053: Summarize workflow lifecycle events

## Context

Lifecycle history can be filtered by action and time, but an operator who only needs archive and
restore totals must still transfer and count every matching event. That adds avoidable database
materialization, response payload, and client logic for a common operational question.

The remaining explicit roadmap item, encrypted tenant credentials, remains blocked by undefined
tenancy, authentication, authorization, credential ownership, and key-rotation contracts. A
bounded summary extends the existing lifecycle API without inventing those security contracts.

## Decision

Add `GET /v1/workflows/{workflow_id}/lifecycle-events/summary`. Return the workflow ID, a total, and
explicit `archived` and `restored` counts. Accept the same optional `created_from` inclusive and
`created_before` exclusive bounds as lifecycle history, including its timezone-awareness and
validation rules.

Use one database query grouped by action rather than materializing event rows. Fill missing action
groups with zero so the response shape is stable for empty and single-action windows. Reuse the
existing workflow/time indexes; no schema migration is required.

## Consequences

- Operators can retrieve compact lifecycle totals without downloading event history.
- Time-bounded totals use the same half-open window semantics as the event list.
- The summary reflects committed rows at query time and is not a snapshot shared with later calls.
- Both action keys are always present, including when their count is zero.
- The endpoint is operational aggregation, not authorization, audit attribution, or retention.
