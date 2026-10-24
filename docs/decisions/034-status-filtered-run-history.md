# ADR 034: Status-filtered workflow run history

## Status

Accepted

## Context

Cursor-paginated history bounds run discovery, but operational clients often need one lifecycle
subset, such as pending work, waiting approvals, or failed executions. Fetching every page and
filtering client-side wastes database, network, and serialization work.

Run status is mutable. Encoding it into the cursor or requiring the cursor row to retain the filter
value would make a valid pagination boundary fail when a worker advances that run.

## Decision

Accept an optional `status` query parameter on `GET /v1/workflows/{workflow_id}/runs`, parsed as the
existing `RunStatus` enum. Apply the exact status predicate to page items while keeping the cursor as
the workflow-scoped `(created_at, id)` boundary established by ADR 033. Clients must repeat the same
status parameter while traversing a result set.

Add a composite `(workflow_id, status, created_at, id)` index for filtered keyset scans. Retain the
existing index for unfiltered history.

## Consequences

- Clients can retrieve bounded pending, running, waiting, completed, or failed histories.
- Invalid status values use FastAPI's standard `422` validation response.
- A cursor remains usable if its run changes status because ordering fields are immutable.
- Pages are not a transactional snapshot: runs may enter or leave the selected state between
  requests. Clients needing a current view should restart at the first page.
- Multi-status and time-range filters remain separate work.
