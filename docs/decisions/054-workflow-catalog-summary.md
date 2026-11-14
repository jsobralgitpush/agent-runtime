# ADR 054: Summarize workflow archive states

## Context

The workflow catalog supports archive-state filtering and bounded traversal, but an operator who
only needs active and archived totals must still download and count workflow definitions. Those
definitions contain complete step graphs, making the list response unnecessarily expensive for a
small operational question.

The remaining explicit roadmap item, encrypted tenant credentials, remains blocked by undefined
tenancy, authentication, authorization, credential ownership, and key-rotation contracts. A catalog
summary improves the current single-tenant API without inventing those security contracts.

## Decision

Add `GET /v1/workflows/summary`. Return a total and explicit `active` and `archived` counts. Accept
the catalog's optional exact, case-sensitive `name` filter so clients can summarize duplicate labels
without loading each definition.

Use one database query grouped by whether `archived_at` is present. Fill missing groups with zero to
keep the response shape stable for empty and single-state catalogs. Reuse the existing archive-state
and name indexes; no migration is required. Register the static summary route before the workflow-ID
route so `summary` cannot be interpreted as an identifier.

## Consequences

- Operators can retrieve compact catalog totals without transferring workflow definitions.
- Exact-name summaries use the same semantics as catalog listing.
- Both state keys are present even when their count is zero.
- Counts reflect committed rows at query time and are not a snapshot shared with later list calls.
- The endpoint is operational aggregation, not tenant authorization or retention policy.
