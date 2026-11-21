# ADR 061: Add opt-in pagination to run recovery events

## Context

Immutable recovery events preserve every expired-lease decision for a workflow run. Repeated worker
failures can grow that history indefinitely, while the initial endpoint returns the complete list.
Offset pagination would become slower on deep histories and can skip or duplicate rows when new
events are appended between requests.

The remaining explicit roadmap item, encrypted tenant credentials, remains blocked by undefined
tenancy, authentication, authorization, credential ownership, and key-rotation contracts. Bounded
access to existing operational history improves the current API without inventing those security
contracts.

## Decision

Add optional `limit` and `cursor` parameters to
`GET /v1/runs/{run_id}/recovery-events`. A limit from 1 to 100 enables keyset pagination ordered by
`(created_at, id)` ascending. Fetch one extra row and, when another page exists, return the last
emitted event ID in `X-Next-Cursor`. The next request supplies that opaque ID with a limit.

Omitting `limit` preserves the original complete list. Reject a cursor without a limit with stable
`422 run_recovery_event_pagination_limit_required`; reject unknown event IDs and events owned by
another run with `422 invalid_run_recovery_event_cursor`. SQLite normalizes timestamp comparisons
with `julianday`, while PostgreSQL retains native indexed comparisons. Reuse the existing
`(run_id, created_at, id)` index; no migration is required.

## Consequences

- Operators can traverse large recovery histories with bounded database and response work.
- Existing clients remain backward compatible until they opt into a limit.
- Appended events do not move the immutable cursor boundary.
- Clients must keep the same run and limit while following cursors.
- Cursors are navigation tokens, not authorization credentials; future tenancy must scope the run
  and cursor event lookup.
- Browser clients must expose `X-Next-Cursor` through CORS before following pages.
