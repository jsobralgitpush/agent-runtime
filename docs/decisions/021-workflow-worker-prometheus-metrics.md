# ADR 021: Expose low-cardinality workflow and worker metrics per process

## Status

Accepted

## Context

HTTP RED metrics show API health but do not reveal whether workflows complete, retry, wait, or fail.
Deferred executions happen in separate worker processes, so adding engine counters only to the API
registry would omit the durable execution path. A shared push gateway or Prometheus multiprocess
directory would introduce deployment and stale-file lifecycle policy beyond this small runtime.

Operational labels must remain bounded. Workflow IDs, run IDs, step keys, worker IDs, provider
names, and arbitrary exception types would create unbounded series and may expose sensitive context.

## Decision

Add process-local Prometheus metrics for:

- workflow engine invocation count and duration, labeled by `synchronous`/`worker` mode and status;
- provider/tool attempt count, labeled by step type and `success`/`error`/`lease_lost` result;
- worker claims and currently active runs; and
- expired-lease recovery count, labeled by `failed`/`requeued` outcome.

The API continues to expose its registry at `/v1/metrics`. A worker starts a dedicated HTTP scrape
server only when `WORKER_METRICS_PORT` is configured; `WORKER_METRICS_HOST` defaults to loopback.
Every worker uses a distinct port and is scraped independently. Do not add resource identifiers,
payloads, provider names, credentials, or error messages to metric labels.

## Consequences

- Operators can alert on workflow failure ratios, latency, retries, worker activity, and recovery.
- Synchronous and deferred execution share one metric contract without pretending to share memory.
- Metrics reset on process restart and aggregate only within the scraped process.
- Multi-worker deployments need one target per process or a future explicit multiprocess strategy.
- Queue depth, stuck-run age, provider-specific errors, and tenant-level service objectives remain
  separate features because they require database queries or additional cardinality policy.
