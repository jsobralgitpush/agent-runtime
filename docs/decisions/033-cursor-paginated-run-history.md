# ADR 033: Cursor-paginated workflow run history

## Status

Accepted

## Context

The runtime persists every workflow run and its step telemetry, but clients can inspect a run only
when they already know its ID. Operational consoles, authoring clients, and debugging tools need to
discover recent runs without reading an unbounded table or maintaining their own ID index.

Offset pagination becomes slower on deep histories and can duplicate or skip rows when new runs are
inserted between requests. Run creation is concurrent, so ordering by timestamp alone also needs a
deterministic tie-breaker.

## Decision

Add `GET /v1/workflows/{workflow_id}/runs`, returning `items` and `next_cursor`. Pages are ordered by
`(created_at, id)` descending, default to 20 items, and accept limits from 1 to 100. The cursor is the
last run ID returned on a non-final page. The API reloads that run, verifies workflow ownership, and
uses its timestamp and ID as the exclusive keyset boundary. Unknown or foreign cursors return a
deterministic `422` response.

Load each run's ordered step records and return the existing `RunRead` representation. Add a
composite `(workflow_id, created_at, id)` index for the access path.

## Consequences

- Clients can discover bounded, stable run history with complete telemetry.
- Newer runs inserted between page requests do not shift older page boundaries.
- Cursor values are opaque navigation tokens, not authorization credentials.
- A cursor becomes invalid if its run is deleted; clients must restart from the first page.
- Filtering by status, time range, or tenant remains separate work.
