# ADR 011: Export opt-in OpenTelemetry traces over OTLP/HTTP

## Status

Accepted.

## Context

Prometheus HTTP metrics describe aggregate service health, and persisted step telemetry describes
completed execution state. Neither connects one HTTP request to its workflow and steps or shows the
timing hierarchy of worker execution. The roadmap calls for OpenTelemetry traces, but tracing must
remain optional for local use and must not copy model payloads into a second data store by default.

## Decision

Enable tracing only when `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` contains the complete OTLP/HTTP traces
URL. In API and worker processes, install an SDK tracer provider with the configured
`OTEL_SERVICE_NAME`, a standard OTLP HTTP exporter, and a batch span processor. Flush the provider
during process shutdown. When the endpoint is absent, do not create an exporter or processor.

Instrument FastAPI with the OpenTelemetry integration. Wrap each engine execution in a
`workflow.run` span and each step in a child `workflow.step` span. Synchronous runs inherit the HTTP
server context; worker runs create root run spans. Record only operational metadata: run and
workflow IDs, execution mode, status, step key/type, attempts, and the successful provider.

Do not attach workflow inputs, prompts, outputs, credentials, or exception messages. Disable the
instrumentation helper's automatic exception recording for engine spans and set failure status with
the exception class only. Leave sampling and exporter authentication to the standard OpenTelemetry
environment variables.

## Consequences

Operators can follow a request through workflow and step timing or inspect worker traces in any
OTLP-compatible backend. The runtime gains an exporter thread only when tracing is enabled, and
batched spans may be lost on abrupt process termination. Run and workflow IDs are intentionally
high-cardinality trace attributes and must not be copied to metric labels. HTTP instrumentation may
emit standard URL and network attributes, so production deployments still need a collector-side
data policy, sampling, retention, and access controls.
