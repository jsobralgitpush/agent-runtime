# ADR 009: Start Prometheus observability with HTTP RED metrics

## Status

Accepted.

## Context

The roadmap includes Prometheus metrics, while the runtime currently exposes operational behavior
only through structured logs and persisted step telemetry. A first metrics slice must be useful for
alerts without introducing labels whose cardinality grows with workflows, runs, or request paths.

API and worker processes do not share memory. Publishing workflow execution counters from the API
would therefore omit deferred runs, while enabling Prometheus multiprocess mode would add deployment
and cleanup policy beyond a small, portable change.

## Decision

Expose a Prometheus endpoint at `/v1/metrics` and instrument all other HTTP requests with:

- total request count by method, matched route template, and status code;
- request duration histogram with the same labels;
- in-progress requests by method.

Use a dedicated in-process collector registry. Exclude the scrape endpoint from its own metrics and
label unmatched routes with the constant `unmatched`. Never label metrics with resource IDs, raw
paths, request payloads, prompts, outputs, or error messages. Normalize non-standard HTTP methods
to `OTHER` so clients cannot create arbitrary method labels.

## Consequences

Operators can build request-rate, error-rate, latency, and saturation dashboards and alerts from a
stable set of series. Metrics reset when the process restarts and represent only the scraped API
process. Deployments with multiple processes must configure one scrape target per process or adopt
Prometheus multiprocess mode. Workflow and worker metrics remain a separate feature because they
need a cross-process collection strategy.
