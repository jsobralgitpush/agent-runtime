# ADR 017: Model human approval as a durable workflow step

## Status

Accepted

## Context

The dependency-aware engine could execute automated work but had no safe boundary for a person to
review an intermediate result. Holding an API request or worker lease while waiting would waste
resources, eventually expire the lease, and leave the decision outside the persisted run history.
Restarting the workflow after review would also risk repeating provider calls or side effects.

## Decision

Add an `approval` step type and a `waiting` run/step status. When an approval step becomes ready,
the engine resolves and persists its input as review context, releases any worker lease, marks the
run waiting, and returns. Approval steps form exclusive planner waves so no later ready step can
start in the same wave.

`POST /v1/runs/{id}/approval` locks the run row and accepts one `approve` or `reject` decision with
an optional comment. Approval completes the step and invokes the engine again; the engine rebuilds
outputs and usage from completed step records and skips them. Rejection fails the approval step and
run. The existing unique `(run_id, step_key)` constraint is retained as a database guard against
duplicate execution history.

Human wait time is not charged to `max_runtime_seconds`. Resumption starts a new active execution
segment because the current schema does not persist cumulative monotonic runtime.

## Consequences

- Review context, decisions, comments, and timestamps are inspectable with the rest of the run.
- Workers do not hold leases or heartbeat tasks while a person decides.
- Multiple gates can pause and resume the same run without replaying completed steps.
- Only one concurrent decision succeeds; later decisions receive a conflict.
- The first version resumes approved work synchronously in the API process.
- Reviewer identity and immutable tenant audit events require authentication and tenancy support and
  remain production follow-up work.
