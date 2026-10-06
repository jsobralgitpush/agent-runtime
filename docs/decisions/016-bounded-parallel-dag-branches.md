# ADR 016: Execute dependency-ready branches in bounded waves

## Status

Accepted.

## Context

Explicit dependencies and deterministic topological planning establish DAG semantics, but every
step still runs serially. Independent provider or tool calls therefore accumulate latency even when
they have no data dependency. Running the existing step coroutine concurrently is unsafe because
SQLAlchemy `AsyncSession` instances are mutable and must not be shared across tasks. Parallel calls
also need defined failure, lease, retry, and budget behavior.

## Decision

Add `max_parallel_steps` to workflow definitions, defaulting to `1` and bounded from `1` to `10`.
Values above one require an explicit dependency graph. Partition the graph into deterministic,
bounded waves of ready steps, preserving declaration order within each wave.

Before a concurrent wave, create every `StepRun`, resolve its input from completed predecessors, and
persist the running state. Concurrent tasks perform only provider or tool calls, timeout handling,
retry delays, tracing, and mutation of their distinct in-memory step result. They never call or hold
the SQLAlchemy session. After all siblings finish, the engine verifies worker lease ownership when
applicable and serially commits the complete wave.

Do not cancel siblings when one branch fails. Persist successful and failed sibling outcomes, fail
the run with the first error in deterministic wave order, and do not start dependent waves. Apply a
single workflow deadline to all tasks. Reconcile token and estimated-cost budgets after every wave;
all already-started calls and their usage remain recorded if their aggregate exceeds a budget.

## Consequences

Independent branches reduce wall-clock latency without unsafe concurrent database access. The
default preserves existing serial behavior, while the explicit cap prevents one workflow from
creating unbounded local concurrency. Retry and tracing behavior remains step-local, and worker
fencing still occurs before final results are committed.

Failure is fail-after-wave rather than fail-fast: a sibling may complete after another branch has
already failed. Token and cost guardrails can overshoot by an entire wave rather than one response.
The limit is per workflow run, not global or tenant-wide, and providers may impose stricter quotas.
Cross-run admission control, provider-specific semaphores, and cancellable remote requests remain
future operational controls.
