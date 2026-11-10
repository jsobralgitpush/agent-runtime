# ADR 049: Persist workflow lifecycle events

## Context

The workflow row exposes only its current `archived_at` value. Restoring a workflow clears that
timestamp, erasing evidence that it was previously unavailable, while repeated archive and restore
requests are intentionally idempotent. Operators need to distinguish real state transitions from
retries without inferring them from application logs or run failures.

The remaining explicit roadmap item, encrypted tenant credentials, is still blocked by undefined
tenancy, authentication, authorization, credential ownership, and key-rotation contracts. A
transition log can improve lifecycle observability without pretending that actor identity exists.

## Decision

Persist an immutable `WorkflowLifecycleEvent` for each successful `archived` or `restored`
transition. Insert the event in the same transaction that changes `archived_at`; failed extension
validation, unknown workflows, and idempotent no-ops append nothing. Use the exact archive timestamp
for both the event and `archived_at`.

Expose `GET /v1/workflows/{workflow_id}/lifecycle-events`, ordered chronologically by
`(created_at, id)`. Add a composite `(workflow_id, created_at, id)` index. Keep events minimal: ID,
workflow ID, action, and timestamp. Do not fabricate an actor or reason before authentication and
request identity exist.

## Consequences

- Operators can reconstruct successful archive/restore sequences after the current state changes.
- Retries remain idempotent and do not pollute the audit sequence.
- State changes and their event records cannot commit independently.
- The log is lifecycle history, not a complete security audit trail without trusted actor context.
- Migration 0013 does not backfill earlier transitions because a current timestamp cannot reveal
  when or how many times a workflow changed state.
- Event listing pagination is defined separately in ADR 050.
