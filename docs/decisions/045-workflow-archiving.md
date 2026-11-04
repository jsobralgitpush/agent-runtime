# ADR 045: Preserve history when archiving workflows

## Context

Workflow definitions are immutable, but every persisted definition remains available for new runs
forever. Operators need to retire obsolete or unsafe definitions without deleting their run history
or changing the definition used by already accepted work. A simple application check would leave a
race in which run creation and archival both appear to succeed without a defined order.

The remaining explicit roadmap item, encrypted tenant credentials, depends on tenancy,
authentication, authorization, credential ownership, and key-rotation contracts that the project
has not defined. Archival is a smaller lifecycle gap that can be closed without inventing that
security model.

## Decision

Add nullable `archived_at` state to workflows and an idempotent
`POST /v1/workflows/{workflow_id}/archive` endpoint. Keep archived definitions visible in catalog
and direct lookup responses, including their timestamp, but reject new run creation with HTTP `409`
and the stable `workflow_archived` code.

Lock the workflow row in both archival and run creation. The transaction that obtains the lock first
defines the boundary: a run is committed before archival, or creation observes `archived_at` and is
rejected. Do not stop pending or active runs after archival. Check run idempotency before the archive
state so a client can still recover a run accepted before the boundary.

Do not add restore or deletion behavior in this change. Unarchiving requires a separate policy for
who may reactivate a definition and whether extensions should be revalidated.

## Consequences

- Obsolete definitions can be made unavailable for new work without losing audit history.
- Existing queued and active runs preserve their accepted execution semantics.
- Repeating the archive action keeps the first timestamp and returns the same state.
- Row locking gives PostgreSQL deployments deterministic ordering for concurrent archive/create
  requests; SQLite remains appropriate for deterministic local development, not cross-process
  coordination.
- Catalog clients must inspect `archived_at`; archival does not hide or delete a workflow.
- Tenant authorization, restore policy, and retention/deletion remain separate work.
