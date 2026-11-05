# ADR 046: Filter the workflow catalog by archive state

## Context

Archived workflows remain visible by design so their definitions and histories stay inspectable.
Once the catalog contains both active and retired definitions, clients that need to start new work
must download every definition and inspect `archived_at` locally. The catalog already supports exact
name filtering, but it cannot select lifecycle state.

The remaining explicit roadmap item, encrypted tenant credentials, depends on tenancy,
authentication, authorization, credential ownership, and key-rotation contracts that the project
has not defined. Archive-state filtering is a smaller catalog gap that can be closed without
inventing that security model.

## Decision

Add an optional `archived` boolean query parameter to `GET /v1/workflows`. When omitted, preserve
the existing full catalog. `archived=false` selects rows whose `archived_at` is null, while
`archived=true` selects rows with a timestamp. Allow the predicate to combine with exact `name`
filtering and preserve the endpoint's newest-first response.

Add an index on `archived_at` so active-only and archived-only catalog scans do not require reading
every workflow row. Do not change the response shape or hide archived workflows by default.

## Consequences

- Authoring clients can request only definitions that accept new runs.
- Operational clients can inspect only retired definitions without local filtering.
- Existing clients continue receiving every workflow when the parameter is absent.
- A nullable timestamp remains the source of truth; no redundant boolean column is introduced.
- The current index assumes a single-tenant catalog. Tenant ownership must lead the relevant indexes
  before exposing the endpoint as a multi-tenant service.
