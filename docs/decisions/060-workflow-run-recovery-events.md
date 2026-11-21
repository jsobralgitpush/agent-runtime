# ADR 060: Persist immutable workflow run recovery events

## Context

Expired worker leases are either failed conservatively or requeued when every interrupted step is
explicitly replay-safe. The worker updates the run and emits logs and process-local metrics, but it
does not retain a durable record of the decision. After ownership is cleared and a replayed run
finishes, operators cannot reconstruct which worker lease expired or how the runtime handled it
without external logs.

The remaining explicit roadmap item, encrypted tenant credentials, remains blocked by undefined
tenancy, authentication, authorization, credential ownership, and key-rotation contracts. Durable
operational recovery history improves the existing worker without inventing those security
contracts.

## Decision

Append one immutable `workflow_run_recovery_events` row in the same transaction that handles each
expired run. Record the run ID, outcome (`requeued` or `failed`), previous lease owner, expired lease
deadline, and recovery timestamp. Index events by `(run_id, created_at, id)` for deterministic
chronological lookup.

Expose `GET /v1/runs/{run_id}/recovery-events` as a read-only chronological list and return `404`
when the run does not exist. A recovery scan that finds no eligible run creates no event, while a
run recovered more than once retains every decision. Deleting a run cascades to its events.

## Consequences

- Operators retain recovery evidence after ownership is cleared or a replayed run completes.
- State changes and their event records commit atomically.
- Worker logs and metrics remain useful for fleet-level alerting, while events support run-level
  diagnosis.
- Events record runtime decisions, not proof that an interrupted external side effect completed.
- Bounded traversal is added separately by ADR 061.
- Future tenant authorization must scope the run before returning its recovery history.
