# ADR 062: Filter workflow run recovery events by outcome

## Context

Recovery history records both replay-safe runs that were requeued and unsafe runs that were failed.
Operators investigating one class of incident must currently retrieve both outcomes and filter them
locally. That wastes database reads and response bytes as immutable histories grow.

The remaining explicit roadmap item, encrypted tenant credentials, remains blocked by undefined
tenancy, authentication, authorization, credential ownership, and key-rotation contracts. A typed
outcome filter improves the existing operational API without inventing those security contracts.

## Decision

Add an optional `outcome` query parameter to
`GET /v1/runs/{run_id}/recovery-events`. Accept only the persisted `requeued` and `failed` enum
values. Apply the predicate before cursor traversal so it composes with chronological keyset
pagination and the unbounded compatibility response.

Add a `(run_id, outcome, created_at, id)` index in migration 0016. Retain the existing
`(run_id, created_at, id)` index because unfiltered history remains supported. Use FastAPI enum
validation for unsupported values and preserve existing response shapes and cursor errors.

## Consequences

- Operators can inspect replay recoveries or conservative failures without client-side filtering.
- Filtered histories retain deterministic chronological pagination and continuation headers.
- Invalid outcomes receive the framework's stable `422` validation response.
- Unfiltered clients remain backward compatible.
- The additional index consumes storage and adds a small write cost to recovery decisions.
- Outcome selection is not authorization or retention; future tenant ownership must still scope the
  run, cursor, and event predicates.
