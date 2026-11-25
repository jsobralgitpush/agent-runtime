# ADR 065: Filter recovery events by expired worker

## Context

Recovery history exposes the worker whose lease expired, but an operator investigating repeated
failures from one worker must currently download every decision for the run and filter locally.
That adds avoidable database and response work as immutable histories grow.

The remaining explicit roadmap item, encrypted tenant credentials, remains blocked by undefined
tenancy, authentication, authorization, credential ownership, and key-rotation contracts. An
expired-worker filter closes a concrete operational query gap without inventing those contracts.

## Decision

Add an optional `expired_lease_owner` query parameter to
`GET /v1/runs/{run_id}/recovery-events`. Match the persisted worker identifier exactly and
case-sensitively, reject empty and overlong identifiers, and apply the predicate before time and
cursor traversal. The filter composes with outcome, creation-time bounds, and opt-in pagination.

Add `(run_id, expired_lease_owner, created_at, id)` while retaining the existing chronological and
outcome indexes. Null owners do not match a named-worker filter.

## Consequences

- Operators can isolate one worker's expired leases without downloading unrelated decisions.
- Exact matching preserves worker identifiers rather than imposing normalization semantics.
- The additional index adds storage and a small write cost for each recovery event.
- Worker identifiers are operational labels, not trusted actor attribution or authorization.
- Deployments must not place secrets in worker identifiers.
