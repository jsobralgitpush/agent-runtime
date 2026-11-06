# ADR 047: Opt-in cursor pagination for the workflow catalog

## Context

The workflow catalog currently returns every persisted definition. Archiving deliberately retains
definitions for auditability, so the response grows monotonically and eventually becomes expensive
for authoring and operational clients. The run-history endpoint already uses keyset pagination to
avoid the shifting and deep-scan behavior of offsets.

Changing the catalog response from a JSON list to a page object would break existing v1 clients.
The remaining explicit roadmap item, encrypted tenant credentials, also remains blocked by
undefined tenancy, authorization, credential ownership, and key-rotation contracts.

## Decision

Add optional `limit` and `cursor` parameters to `GET /v1/workflows`. Supplying a limit from 1 to 100
enables keyset pagination ordered by `(created_at, id)` descending. Fetch one extra row and, when a
subsequent page exists, return the last emitted workflow ID in the `X-Next-Cursor` response header.
The next request supplies that value as `cursor` together with `limit`.

Allow pagination to compose with exact-name and archive-state filters. Unknown cursors receive a
stable `422 invalid_workflow_cursor` response, and a cursor without a limit receives
`422 workflow_pagination_limit_required`. Add a `(created_at, id)` index for the unfiltered access
path. When `limit` is omitted, retain the existing unbounded list response and emit no cursor.
Normalize SQLite timestamp comparison with `julianday` because its database-generated timestamps
and Python-bound values use different textual precision; PostgreSQL keeps the indexed native
timestamp comparison.

## Consequences

- Large catalogs can be traversed in bounded, stable pages without changing the JSON item shape.
- New workflows inserted between requests do not shift older page boundaries.
- Existing clients continue receiving the complete list when they do not opt into pagination.
- The continuation token is carried in a header, so browser deployments must expose that header in
  their CORS policy.
- A workflow ID is an opaque navigation token, not proof of access. Future tenant ownership must
  scope both cursor lookup and page selection.
- The compatibility path remains unbounded; clients must opt in to receive bounded responses.
