# ADR 067: Filter recovery events by lease expiry

## Context

Recovery event creation time records when a healthy worker processed an expired run. It can be
later than the actual lease deadline because workers recover leases while polling bounded batches.
Operators investigating an incident window therefore cannot reliably select leases that expired
during that window by filtering on recovery creation time.

The remaining explicit roadmap item, encrypted tenant credentials, remains blocked by undefined
tenancy, authentication, authorization, credential ownership, and key-rotation contracts. A lease
deadline filter closes a concrete operational query gap without inventing those contracts.

## Decision

Add optional `lease_expired_from` and `lease_expired_before` parameters to
`GET /v1/runs/{run_id}/recovery-events`. Define a timezone-aware half-open interval with an
inclusive lower bound and exclusive upper bound. Normalize offsets to UTC, reject naive timestamps
and reversed ranges, and apply the predicates before cursor traversal.

Keep results and cursors ordered by recovery `(created_at, id)`. Add
`(run_id, lease_expired_at, created_at, id)` to bound run-scoped lease-expiry ranges while retaining
the existing chronological, outcome, and owner indexes.

## Consequences

- Operators can investigate leases that actually expired during an incident window.
- Lease-expiry bounds compose with outcome, expired owner, recovery-time bounds, and pagination.
- Adjacent windows do not overlap because the upper bound is exclusive.
- Results remain ordered by recovery time rather than lease deadline.
- The additional index adds storage and a small write cost for each recovery event.
- Clock skew in the recorded lease deadline can affect interval membership.
