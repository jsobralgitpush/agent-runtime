# ADR 044: Database-enforced workflow creation idempotency

## Context

Run creation is idempotent, but workflow creation is not. If an authoring client loses the response
to `POST /v1/workflows`, retrying creates another immutable workflow with a different ID. A
read-before-write check alone cannot protect concurrent requests handled by separate API processes.

The remaining explicit roadmap item, encrypted tenant credentials, depends on tenancy,
authentication, authorization, credential ownership, and key-rotation contracts that the project
has not defined. Request idempotency is a smaller reliability gap that can be closed without
inventing that security model.

## Decision

Accept an optional `Idempotency-Key` header, bounded to 1 through 255 characters, on workflow
creation. Persist it on the workflow and enforce uniqueness with a database index. Check for an
existing key before extension preflight, then handle a concurrent uniqueness failure by rolling back
and loading the winning row.

A replay returns the original workflow only when its name, description, and normalized definition
match the new request. Reusing the key for a different payload returns HTTP `409` with the stable
`idempotency_key_reused` code. Do not expose the key in the workflow response. Requests without the
header preserve the existing non-idempotent behavior.

## Consequences

- Clients can safely retry ambiguous workflow-creation requests without producing duplicates.
- Database uniqueness protects races across processes rather than relying on an application check.
- A changed provider or tool registry does not invalidate a matching replay of an already committed
  workflow because lookup happens before extension preflight.
- A key is an identifier, not a secret or authorization capability.
- The current index is globally unique for the single-tenant schema; tenancy must replace it with a
  `(tenant_id, idempotency_key)` constraint before external multi-tenant use.
