# ADR 051: Filter workflow lifecycle events by action

## Context

The lifecycle endpoint can return archive and restore transitions in bounded pages, but operational
clients interested in one transition type must still download both and filter locally. As immutable
histories grow, that wastes database reads and network transfer. The existing chronological index
does not provide an action-leading access path after workflow ownership is established.

The remaining explicit roadmap item, encrypted tenant credentials, remains blocked by undefined
tenancy, authentication, authorization, credential ownership, and key-rotation contracts. A typed
event filter improves the existing lifecycle API without inventing those security contracts.

## Decision

Add an optional `action` query parameter to
`GET /v1/workflows/{workflow_id}/lifecycle-events`. Accept only the persisted `archived` and
`restored` enum values. Apply the predicate before cursor traversal so filtering composes with the
existing chronological keyset pagination and compatibility response.

Add a `(workflow_id, action, created_at, id)` index in migration 0014. Retain the existing
`(workflow_id, created_at, id)` index because unfiltered history remains a supported access path.
Use FastAPI's enum validation for unsupported values and preserve every existing response shape.

## Consequences

- Operators can retrieve only archive or only restore transitions without client-side filtering.
- Filtered histories retain deterministic chronological pagination and continuation headers.
- Invalid actions receive the framework's stable `422` validation response.
- Unfiltered clients remain backward compatible.
- The additional index consumes storage and adds a small write cost to lifecycle transitions.
- Action selection is not authorization or retention; future tenant ownership must still scope the
  workflow, cursor, and event predicates.
