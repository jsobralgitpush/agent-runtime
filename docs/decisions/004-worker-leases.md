# ADR 004: Track worker ownership with renewable leases

## Status

Accepted

## Context

A database worker marks a run as running before executing it. Without ownership and liveness data,
operators cannot distinguish slow work from a worker that died after claiming a run.

## Decision

Store a worker ID, heartbeat timestamp, and lease deadline on each claimed run. The worker renews
the lease from a separate database session while the engine is busy and releases the active lease
when execution finishes. Lease duration and heartbeat frequency are configurable.

## Consequences

- Run inspection exposes which worker owns active work and when its lease expires.
- A worker crash leaves an expired, observable lease instead of an indefinitely ambiguous run.
- Lease renewal does not compete with the engine's session during long provider or tool calls.
- This change does not reclaim expired work. Automatic recovery requires resumable step semantics
  and side-effect idempotency, so it remains a separate change.
