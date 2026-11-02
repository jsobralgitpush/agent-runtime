# ADR 043: Exact workflow-name catalog filter

## Context

`GET /v1/workflows` returns the complete workflow catalog. Authoring and operational clients that
know a workflow's display name must download every definition and filter locally, even though the
database already indexes that field. Workflow names are not unique, so treating a name as a direct
resource identifier would also hide valid matches.

The remaining explicit roadmap item, encrypted tenant credentials, depends on tenancy,
authentication, authorization, credential ownership, and key-rotation contracts that the project
has not defined. This smaller catalog gap can be closed without inventing that security model.

## Decision

Add an optional `name` query parameter to `GET /v1/workflows`. When present, require 1 to 120
characters, matching workflow creation bounds, and apply an exact, case-sensitive equality
predicate. Return every matching workflow in the endpoint's existing newest-first order. A missing
name returns an empty list; omitting the parameter preserves the complete catalog response.

Reuse the existing workflow-name index. Do not make names unique, add fuzzy matching, or reinterpret
the name as an authorization capability.

## Consequences

- Clients can discover workflows by a known display name without transferring unrelated
  definitions.
- Duplicate names remain valid and visible, so clients must use workflow IDs for later operations.
- Exact matching has deterministic, index-friendly semantics across supported databases.
- Partial, case-insensitive, and ranked search remain separate product decisions.
- A future multi-tenant service must add tenant ownership to both the predicate and its supporting
  index before exposing catalog lookup externally.
