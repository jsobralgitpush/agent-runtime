# ADR 038: Workflow run lookup by idempotency key

## Status

Accepted

## Context

Run creation uses a workflow-scoped idempotency key and a database unique constraint, so retrying a
`POST` after an ambiguous network failure returns the original run. Some clients need a read-only
recovery path: replaying a creation request is operationally surprising when the goal is only to
discover whether the first request reached the service.

## Decision

Add an optional exact `idempotency_key` query parameter to
`GET /v1/workflows/{workflow_id}/runs`. Apply it with the workflow predicate and the existing status,
time, and cursor filters. Return the standard history page containing at most one run because the
database already enforces `(workflow_id, idempotency_key)` uniqueness. A missing key returns an
empty page rather than `404`, matching other history filters.

Limit the query value to the same 255 characters accepted by the creation header. Reuse the unique
constraint's index; no migration or second lookup endpoint is necessary.

## Consequences

- Clients can recover a run ID without issuing another creation request.
- The lookup returns full run and step telemetry through the existing response contract.
- The same key can identify different runs in different workflows, so workflow scoping is required.
- Keys are identifiers, not credentials; authentication and tenant authorization remain required in
  a multi-tenant deployment.
- Clients should not place secrets or personal data in idempotency keys.
