# ADR 019: Expose stable per-step operation keys through runtime references

## Status

Accepted

## Context

Run creation is idempotent, but a worker can lose its lease after an external side effect succeeds
and before the result is persisted. Replaying that step is unsafe unless the downstream operation
can recognize the duplicate. Workflow authors already control tool inputs, but they had no supported
way to include the runtime-generated run ID in an idempotency key.

Changing every tool callable to accept a new context argument would break the intentionally narrow
tool protocol. Automatically retrying side effects based only on a boolean declaration would claim
safety that the runtime cannot verify.

## Decision

Extend the reference model with three engine-owned string values:

- `${runtime.run_id}`
- `${runtime.step_key}`
- `${runtime.operation_key}`, formatted as `run_id:step_key`

Resolve runtime references with the rest of the step input before its first attempt. The resolved
input is persisted on `StepRun`, and the same value is reused by retries. Because both identifiers
are durable, the operation key is also stable when a workflow resumes from completed checkpoints.

Tools remain ordinary allowlisted async callables. A workflow explicitly places the operation key
where its downstream API, database constraint, or outbox expects it.

## Consequences

- Side-effecting integrations can implement idempotency without changing the tool registry contract.
- Operation keys are deterministic, inspectable in resolved step input, and unique within a run.
- LLM, tool, approval, serial, and parallel steps share one reference contract.
- The runtime supplies identity but cannot prove downstream enforcement.
- Replay remains disabled by default. [ADR 020](020-opt-in-replay-safe-recovery.md) adds an explicit
  workflow-author contract for steps whose downstream idempotency policy makes replay acceptable.
