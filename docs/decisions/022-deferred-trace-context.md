# ADR 022: Continue traces across the database worker boundary

## Status

Accepted.

## Context

FastAPI requests and synchronous workflow execution already share an OpenTelemetry trace. Deferred
runs cross a database boundary and may execute much later in another process, so the worker
previously created a new root trace. Operators could correlate it manually with run IDs, but could
not navigate one trace from enqueue or approval through workflow and step execution.

Copying arbitrary propagation headers would create an unnecessary persistence surface. In
particular, OpenTelemetry baggage may contain application-defined or sensitive values and is not
required to establish W3C parentage.

## Decision

When a request defers a new run, inject the current propagation context and persist only
`traceparent` and `tracestate` on the run. When an approved continuation is deferred, replace those
fields with the decision request's context. Before worker execution, validate and attach the stored
context so `workflow.run` becomes its child; detach it after execution so context cannot leak to the
next run.

Keep the propagation state internal: it is nullable, is not part of `RunRead`, and is ignored when
absent or invalid. Do not persist baggage or arbitrary request headers. Synchronous execution keeps
its existing in-process context behavior.

## Consequences

Trace backends can show the HTTP request, deferred workflow, and steps in one trace despite process
and time boundaries. A deferred approval starts the continuation under the approval request, which
is the action that actually re-enters the queue. Lease recovery may create another child of the same
persisted parent; run identifiers distinguish repeated execution attempts.

The database now contains trace identifiers as operational metadata and needs matching retention
and access controls. Trace continuity remains best effort: disabled instrumentation, unsampled or
invalid context, and non-W3C propagators may result in no stored parent.
