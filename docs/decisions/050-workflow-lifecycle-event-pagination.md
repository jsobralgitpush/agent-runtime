# ADR 050: Opt-in pagination for workflow lifecycle events

## Context

Lifecycle events are immutable and deliberately retained, so a workflow repeatedly archived and
restored accumulates an unbounded history. The initial endpoint returns the complete chronological
list, which is convenient for small histories but eventually transfers and materializes every event.
Changing the response to a page object would break existing v1 clients.

The remaining explicit roadmap item, encrypted tenant credentials, remains blocked by undefined
tenancy, authentication, authorization, credential ownership, and key-rotation contracts. Bounded
lifecycle reads improve an existing API without inventing those security contracts.

## Decision

Add optional `limit` and `cursor` parameters to
`GET /v1/workflows/{workflow_id}/lifecycle-events`. A limit from 1 to 100 enables keyset pagination
over the existing `(workflow_id, created_at, id)` index in chronological order. Fetch one extra row
and, when another page exists, emit the last returned event ID in `X-Next-Cursor`. The next request
passes that ID as `cursor` together with a limit.

Reject a cursor without a limit with
`422 workflow_lifecycle_event_pagination_limit_required`. Reject unknown event IDs and events owned
by another workflow with `422 invalid_workflow_lifecycle_event_cursor`. Normalize SQLite timestamp
comparison with `julianday` to keep equal timestamps stable across textual precision differences;
PostgreSQL retains native indexed datetime comparison. When limit is omitted, preserve the complete
JSON list and emit no cursor.

## Consequences

- Clients can traverse long histories in bounded, stable pages without changing event objects.
- Events appended during traversal do not shift already returned chronological boundaries.
- Existing clients continue receiving the complete history until they opt in.
- The compatibility path remains unbounded and clients must opt in for bounded reads.
- Browser deployments must expose `X-Next-Cursor` through CORS.
- Cursor IDs are navigation tokens, not proof of access; future tenant authorization must scope the
  workflow lookup, cursor lookup, and page predicate.
- The existing composite index supports this query, so no schema migration is required.
