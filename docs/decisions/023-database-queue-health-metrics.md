# ADR 023: Observe database queue depth and oldest pending age

## Status

Accepted

## Context

The runtime exposes HTTP, workflow, step, and worker lifecycle metrics, but none answers whether
deferred work is accumulating or how long it has waited. Claim counters may stay flat both when the
queue is empty and when workers cannot keep up, so they cannot support backlog or stuck-queue
alerts by themselves.

The database is the durable queue and therefore the authoritative source. Adding run IDs, workflow
IDs, or per-tenant labels would create unbounded series, while querying the database independently
from every Prometheus collection callback is not compatible with the worker's synchronous metrics
server.

## Decision

Add two unlabeled gauges:

- `agent_runtime_queue_depth`, the number of pending workflow runs; and
- `agent_runtime_queue_oldest_pending_age_seconds`, the non-negative age of the oldest pending run,
  or zero when the queue is empty.

Refresh both values with one aggregate database query. The async API refreshes before rendering
each metrics response. A worker with `WORKER_METRICS_PORT` configured refreshes after every claim or
idle poll and during lease heartbeats, avoiding database work when worker metrics are disabled.

## Consequences

Operators can alert on sustained backlog size and waiting time without high-cardinality labels.
Every process exposes its latest view of the same shared queue, so dashboards must select one target
or use a non-additive aggregation such as `max`; summing workers would overcount. API values are
scrape-time current. Worker values can lag by up to the idle poll interval or, during execution, the
heartbeat interval, and reset to zero on process restart until their first refresh.

This does not classify why work is delayed, measure provider saturation, or define alert thresholds.
Those remain deployment and provider-reliability policies.
