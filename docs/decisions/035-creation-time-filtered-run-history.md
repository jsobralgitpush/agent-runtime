# ADR 035: Creation-time-filtered workflow run history

## Status

Accepted

## Context

Status-filtered history helps operational views, but clients still need to scan every run in that
state when investigating a known incident window or exporting a bounded period. ADRs 033 and 034
leave time-range filtering as separate work.

Timestamps without an offset depend on database or process timezone and make API results ambiguous.
Closed upper bounds also overlap when clients split adjacent windows.

## Decision

Add optional `created_from` and `created_before` query parameters to workflow run history. Define a
half-open interval: `created_from` is inclusive and `created_before` is exclusive. Require both
values to carry a timezone offset, normalize them to UTC, and reject a lower bound that is equal to
or later than the upper bound with a deterministic `422 invalid_run_time_range` response.

Apply the interval before the existing `(created_at, id)` keyset boundary. Clients must repeat the
same time bounds while following cursors. Existing unfiltered and status-filtered history indexes
already cover these predicates, so no schema change is required.

## Consequences

- Clients can query incident and export windows without scanning unrelated history.
- Adjacent half-open ranges do not duplicate runs at their shared boundary.
- Timezone handling is explicit and normalized across clients.
- Cursors remain workflow-scoped ordering boundaries rather than carrying filter state.
- Time filtering does not delete data or replace a retention policy.
- Completion-time filtering remains separate work; ADR 037 adds it independently.
