# ADR 036: Multi-status workflow run history

## Status

Accepted

## Context

An exact status filter avoids scanning unrelated run history, but operational clients commonly need
sets such as every active run (`pending`, `running`, and `waiting`). Making one request per state
complicates clients because they must merge multiple independently paginated timelines while
preserving the global newest-first order. ADR 034 leaves multi-status filtering as separate work.

## Decision

Allow clients to repeat the existing `status` query parameter on
`GET /v1/workflows/{workflow_id}/runs`. Parse every value through `RunStatus` and apply one SQL `IN`
predicate before the existing time and keyset boundaries. A single `status` value remains valid and
backward compatible. Clients must repeat the complete status set while following a cursor.

Keep the workflow-scoped `(created_at, id)` cursor unchanged. The existing
`(workflow_id, status, created_at, id)` index supports each member of the bounded status set, so no
schema migration is needed.

## Consequences

- Clients can retrieve and paginate globally ordered run sets without merging separate histories.
- Unknown values continue to use FastAPI's standard `422` validation response.
- Status changes between page requests can move runs into or out of the selected set; pagination is
  not a transactional snapshot.
- The cursor does not encode filter values. Clients are responsible for keeping the status set and
  any creation-time bounds unchanged across pages.
