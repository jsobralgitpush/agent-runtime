# ADR 003: Start asynchronous execution with a database-backed worker

## Status

Accepted

## Context

The synchronous API is useful for demos but couples request duration to workflow duration. The
runtime already persists runs in PostgreSQL, and v0.2 needs a worker boundary before leases and
crash recovery can be introduced.

## Decision

Keep synchronous execution as the default and honor `Prefer: respond-async` as an opt-in. Deferred
runs remain pending until an `agent-worker` process claims them. Workers use an ordered row-locking
query with `SKIP LOCKED`, allowing multiple processes to claim different runs. Both paths call the
same `WorkflowEngine`.

## Consequences

- Existing clients keep their current response behavior.
- Deferred clients receive `202 Accepted` and can poll the run endpoint.
- PostgreSQL acts as the initial queue, avoiding a second infrastructure dependency.
- Worker crashes can currently leave a run in `running`; leases, heartbeats, and recovery are a
  required follow-up before production use.
