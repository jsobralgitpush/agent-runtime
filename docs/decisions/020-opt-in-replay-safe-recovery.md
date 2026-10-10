# ADR 020: Requeue expired runs only through an explicit replay-safety contract

## Status

Accepted

## Context

Lease expiration proves that the current worker must be fenced, but it does not reveal whether the
external provider or tool call finished. Always failing leaves safely repeatable work unavailable;
always replaying can duplicate a side effect. Stable per-step operation keys give integrations a
way to deduplicate calls, but the runtime cannot observe whether a downstream system enforces them.

Recovery must also preserve completed DAG branches and the existing `(run_id, step_key)` uniqueness
constraint. Creating a second step record would lose the checkpoint model and violate that
constraint.

## Decision

Add `replay_safe`, defaulting to `false`, to provider and tool steps. During the bounded expired-lease
sweep:

- requeue a run when it has no started step or every pending/running step declares `replay_safe`;
- otherwise retain the existing terminal `WorkerLeaseExpired` failure;
- clear the expired ownership before either outcome so the old worker is fenced; and
- reset replayable incomplete records to `pending` while preserving completed step records.

The engine reuses an existing incomplete `StepRun` on resume and adds new attempts to its persisted
attempt count. Workflow authors should pair side-effecting replay-safe steps with
`${runtime.operation_key}` and enforce that key downstream. Approval steps cannot declare replay
safety because entering `waiting` releases the lease and does not require this recovery path.

## Consequences

- Default behavior remains backward compatible and never adds a new attempt after lease expiry.
- Opted-in calls have at-least-once execution and can recover automatically after worker death.
- Completed serial steps and DAG siblings are not repeated.
- The runtime records cumulative attempts but does not yet store a separate immutable recovery event.
- An incorrect `replay_safe` assertion can duplicate side effects; the runtime cannot validate an
  external idempotency implementation.
