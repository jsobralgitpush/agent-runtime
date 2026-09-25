# ADR 005: Fail expired runs without automatic replay

## Status

Accepted

## Context

A dead worker leaves a run in `running` after its lease expires. Reclaiming and immediately
replaying that run is unsafe: the interrupted provider or tool call may have completed externally
even though its local result was never persisted.

## Decision

Before claiming pending work, workers sweep expired leases in bounded, row-locked batches. An
expired run becomes `failed`, and any pending or running step is also marked failed with a stable
`WorkerLeaseExpired` error. Completed steps are preserved. Recovery clears active ownership, and
workers transactionally verify their lease before persisting every step result. A late worker is
therefore fenced out. The runtime does not replay any step automatically.

## Consequences

- Interrupted work reaches a clear terminal state instead of remaining indefinitely `running`.
- Multiple workers can sweep concurrently without handling the same run.
- Existing completed step history and the last heartbeat remain inspectable.
- A late result from the expired worker cannot overwrite the recovered state.
- Retrying or resuming requires a future explicit operation with side-effect idempotency rules.
