# ADR 048: Revalidate and restore archived workflows

Lifecycle transition auditing is defined separately in ADR 049.

## Context

Workflow archival deliberately preserves immutable definitions and run history, but it is currently
one-way. An accidental archive or a temporarily disabled definition must be recreated under a new
ID, fragmenting its operational history. ADR 045 deferred restoration until its reactivation policy
could be defined separately.

The remaining explicit roadmap item, encrypted tenant credentials, is still blocked by undefined
tenancy, authentication, authorization, credential ownership, and key-rotation contracts.
Restoration closes the existing lifecycle gap without inventing that security model.

## Decision

Add an idempotent `POST /v1/workflows/{workflow_id}/restore` endpoint. Lock the workflow row using
the same mechanism as archive and run creation. For an archived workflow, parse its immutable stored
definition and revalidate every provider and tool reference against the current runtime registry.
Only after validation succeeds, clear `archived_at` and commit. An already active workflow is a
successful no-op.

The shared row lock defines the boundary with new run creation: restoration commits before the run
and the run may proceed, or run creation observes the archived timestamp and returns
`409 workflow_archived`. Do not modify, replay, or delete any existing runs during restoration.

## Consequences

- Accidental or temporary archival can be reversed without changing workflow identity or history.
- Definitions referencing unavailable providers or tools remain archived with a stable validation
  error rather than becoming active and failing every new run.
- Repeating restore is safe and returns the active workflow unchanged.
- Registry validation is local configuration validation, not a provider health check.
- Tenant authorization remains required before archive and restore can be exposed as privileged
  multi-tenant operations.
- SQLite remains suitable for deterministic local tests; cross-process ordering relies on a
  database with effective row-level locks such as PostgreSQL.
