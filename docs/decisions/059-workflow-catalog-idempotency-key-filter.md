# ADR 059: Recover workflows by creation idempotency key

## Context

Workflow creation uses a database-enforced idempotency key so clients can safely repeat a request
after losing its response. Recovery currently requires the client to retain and resend the entire
original payload; the catalog can filter by display name, but names are intentionally non-unique.
An operator or client that retains only its request key cannot locate the committed workflow.

The remaining explicit roadmap item, encrypted tenant credentials, remains blocked by undefined
tenancy, authentication, authorization, credential ownership, and key-rotation contracts. Exact
lookup over an already persisted, indexed key improves the current single-tenant API without
inventing those security contracts.

## Decision

Add an optional, exact `idempotency_key` filter to `GET /v1/workflows`, with the same 1-to-255
character bounds as the creation header. Apply it before ordering and keyset traversal, and allow it
to compose with exact name, archive state, creation time, archive time, limit, and cursor.

Reuse the existing globally unique `ix_workflows_idempotency_key` index. The response remains the
catalog's list shape for compatibility, but uniqueness limits a matching result to zero or one item
and therefore prevents a next-page cursor. Workflows created without an idempotency key do not match
an exact-key request. No migration is required.

## Consequences

- Clients can recover a lost creation response without resubmitting the workflow definition.
- Existing clients remain backward compatible when the filter is omitted.
- Exact lookup does not expose the idempotency key in workflow responses.
- Keys remain request identifiers rather than credentials and must not contain secrets or personal
  data.
- Future multi-tenant support must scope both uniqueness and lookup by authenticated ownership.
