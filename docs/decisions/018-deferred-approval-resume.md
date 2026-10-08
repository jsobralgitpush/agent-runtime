# ADR 018: Requeue approval continuations on explicit preference

## Status

Accepted

## Context

Approval gates persist checkpoints safely, but the first implementation resumes approved work in
the HTTP request. A long continuation can therefore exceed proxy deadlines even when the run first
reached the gate through a worker. Automatically queueing every approval would avoid that coupling,
but it would also break clients that rely on the synchronous response and deployments that do not
run a worker. The schema does not persist the run's original execution mode.

## Decision

Honor `Prefer: respond-async` on an approve request, matching run creation. After locking the run and
persisting the approval decision, transition it from `waiting` to `pending` and return `202 Accepted`.
The existing worker claim path obtains a new lease and calls the resume-aware engine, which rebuilds
outputs and budgets from completed step runs and executes only the remaining plan.

Without the preference, approval retains synchronous behavior. Rejection ignores the preference
because it terminalizes the run and has no continuation to schedule.

## Consequences

- Long approval continuations no longer need to hold an HTTP request open.
- The queue, lease, heartbeat, tracing, and recovery behavior is reused without a second scheduler.
- Persisted checkpoints and the `(run_id, step_key)` constraint prevent completed work from replaying.
- Clients explicitly choose between an immediate result and a durable pending response.
- This is a safe checkpoint-resume slice, not general worker-crash recovery; interrupted external
  side effects still must not be replayed without an idempotency contract.
