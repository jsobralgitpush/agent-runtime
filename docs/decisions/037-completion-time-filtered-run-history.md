# ADR 037: Completion-time-filtered workflow run history

## Status

Accepted

## Context

Creation-time ranges answer when runs entered the system, but incident review and reporting often
need runs that became terminal during a period. A long-running workflow can be created before an
incident window and complete inside it, so creation time is not a substitute for completion time.
PR #33 explicitly leaves completion-time filtering as separate work.

## Decision

Add optional `completed_from` and `completed_before` query parameters to workflow run history. Use
the same timezone-aware, UTC-normalized, half-open interval rules as creation-time filtering. A
lower bound is inclusive, an upper bound is exclusive, and an empty or reversed interval returns
the deterministic `422 invalid_run_time_range` response.

Apply completion predicates before the existing `(created_at, id)` keyset boundary. Comparisons
against nullable `completed_at` naturally exclude pending, running, and waiting runs. Preserve the
endpoint's newest-created-first order and cursor contract rather than introducing a second ordering
mode. Add `(workflow_id, completed_at)` to bound the completion-range scan; the database may sort
the bounded matches by the existing history order.

## Consequences

- Clients can retrieve runs terminalized in an incident or reporting window.
- Creation and completion filters can be combined to express both lifecycle boundaries.
- Completion-filtered pages remain ordered by creation time, matching every other history request.
- Clients must repeat all bounds and statuses while following a cursor.
- The filter does not delete data or replace a retention policy.
