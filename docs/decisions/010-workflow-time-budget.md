# ADR 010: Bound workflow execution time cumulatively

## Status

Accepted.

## Context

Per-step timeouts bound one attempt, but a workflow with many steps and retries can still occupy an
API or worker process far longer than intended. The roadmap includes budgets; token and cost budgets
depend on provider usage reporting and pricing policy, while elapsed execution time is available for
every provider and tool.

## Decision

Allow a workflow definition to set `max_runtime_seconds` greater than zero and no more than 3,600
seconds. Measure the budget with a monotonic clock from immediately before step execution. Charge
elapsed engine time across steps and retry backoffs to the shared budget, in both synchronous and
deferred execution.

For every attempt, use the smaller of its step timeout and the workflow's remaining budget. When the
workflow budget expires, cancel the active call, persist `WorkflowTimeBudgetExceededError` on the
step and run, and do not start later steps. Preserve outputs from steps that completed earlier.

Check the deadline at safe execution boundaries. Do not cancel a database commit midway through a
transaction; allow it to finish, then count that elapsed time when deciding whether another call may
start.

## Consequences

Operators can bound run occupancy consistently across provider and tool implementations. A timed-out
external operation may still have produced a side effect, just as with the existing per-step timeout;
tools still need stable idempotency keys. The budget is process-local enforcement, not a tenant quota.
Token and cost budgets remain separate work.
