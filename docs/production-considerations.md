# Production considerations

v0.1 demonstrates production concerns but is not presented as a finished multi-tenant service. This document distinguishes implemented safeguards from the next operational layer.

## Already implemented

- Database uniqueness is the source of truth for request idempotency.
- Provider and tool implementations are allowlisted, not dynamically imported.
- Workflow size, identifiers, retries, and timeouts have validation bounds.
- Workflows can bound cumulative execution time, reported LLM token usage, and estimated cost.
- Step dependency graphs are validated and planned deterministically before execution.
- Dependency-ready provider and tool calls can run in bounded concurrent waves.
- Human approval gates persist review context and an approve/reject decision before resuming.
- Each execution boundary records status, attempts, latency, output, usage, cost, and errors.
- `/v1/metrics` exposes low-cardinality HTTP, workflow execution, and step-attempt metrics.
- Workers optionally expose process-local claim, active-run, recovery, workflow, and step metrics.
- Optional OTLP/HTTP tracing correlates API requests with workflow and step execution.
- JSONL evaluation datasets can exercise persisted workflows with exact expected outputs.
- Separate liveness and database-backed readiness endpoints support process restart and traffic
  routing decisions without probing external providers.
- The container uses a non-root user.
- CI has read-only repository permissions and a frozen dependency installation.
- The fake provider enables deterministic tests without sending data externally.

## Required before external traffic

Configure the process liveness probe against `/v1/health` and the traffic readiness probe against
`/v1/ready`. Database readiness is intentionally narrow: it does not prove migrations are current,
workers are consuming the queue, providers are reachable, or tenant configuration is valid. Monitor
those concerns separately and protect operational endpoints according to the deployment network
policy.

### Queueing and recovery

Deferred execution uses database-backed workers with renewable leases and heartbeats. Expired runs
are terminalized as failed by default, preventing them from remaining ambiguously `running`. A step
can opt into at-least-once recovery with `replay_safe`; the worker requeues an expired run only when
every interrupted step carries that declaration. Completed checkpoints are preserved and attempts
remain cumulative. This policy can repeat a call that completed remotely but was not persisted, so
production workflows must document and test the corresponding idempotency strategy.

### DAG concurrency

Parallel branches are explicitly bounded to ten calls and concurrent tasks never share the
SQLAlchemy session. A failed branch does not cancel already-started siblings; their results remain
available for diagnosis, but dependent waves do not start. Token and cost budgets are reconciled at
wave boundaries and can overshoot by the combined usage of one wave. For stronger production
isolation, add provider-specific concurrency limits, admission control across runs, prompt
cancellation where APIs support it, and database-backed accounting for tenant-wide quotas.

### Side-effect idempotency

Run creation idempotency does not make arbitrary tools idempotent. Workflow inputs can include
`${runtime.operation_key}` to receive a stable `run_id:step_key` value. Pass it to a downstream
idempotency header or unique constraint, or implement an outbox. The runtime cannot verify that a
custom tool actually enforces the key, so interrupted side effects remain non-replayable by default.
Setting `replay_safe` is an explicit assertion by the workflow author, not automatic proof. A false
assertion can duplicate an external effect. Recovery does not yet persist a separate immutable event
for every lease expiration; operators must retain worker logs and the last heartbeat for diagnosis.

### Human approvals

Approval gates prevent later workflow steps from starting, and row locking makes the first valid
decision authoritative. The decision and optional comment are stored on the step run. Approvals can
use `Prefer: respond-async` to re-enter the durable worker queue; without it they resume in the API
process for backward compatibility. Before external traffic, protect the endpoint with tenant-scoped
authorization and persist the authenticated reviewer identity plus a separate immutable audit event;
a free-form comment is not an identity. Active workflow time budgets restart after approval and
intentionally exclude human waiting time.

### Authentication and tenancy

Add authentication, tenant ownership columns, row-level authorization, rate limits, payload limits, and audit events. Every query must be scoped by tenant.
Run-history cursors are run IDs, not authorization capabilities. Validate workflow and cursor
ownership inside the tenant boundary, retain the 100-item page limit, and define retention or
archival before histories grow without bound.
Idempotency keys are also not authorization capabilities. Scope exact-key lookups to the
authenticated tenant and avoid embedding secrets or personal data in client-generated keys.
Run summaries are operational metadata and require the same workflow ownership check as detailed
history. They are point-in-time aggregates, not a snapshot guarantee for subsequent page requests,
and should not be exposed as a high-cardinality Prometheus label set. Creation-time bounds restrict
the aggregate but do not provide retention, deletion, or immutable reporting snapshots.
Completion-time bounds exclude nonterminal runs and report current terminal status, not a historical
status transition ledger.
Status-filtered pages are not a database snapshot: runs can change lifecycle state between requests.
Clients should keep the complete status set unchanged while following cursors and refresh from the
first page when they need a current operational view.
Creation-time bounds restrict query results but are not a retention control. Continue to archive or
delete historical runs according to an explicit data-lifecycle policy.
Completion-time bounds also remain query filters rather than retention controls. Runs without a
terminal timestamp are intentionally absent from completion-filtered results.
Provider and tool discovery endpoints must use that same authorization boundary in a multi-tenant
deployment. Knowing a tool name is not permission to execute it.
Creation-time extension validation reflects the API process only. Keep provider credentials and
custom registry configuration consistent across API and worker processes; otherwise a workflow can
pass preflight and still fail when a worker resolves an extension.
The validation-only endpoint has the same registry scope and accepts the full workflow creation
payload. Apply the same authentication, rate limits, and payload limits as workflow creation even
though it does not persist data or execute extensions.

### Secret handling

The OpenAI and Anthropic adapters accept environment-injected API keys and never persist them in
workflow data. For production, source provider credentials from a cloud secret manager rather than
a checked-in `.env` file. Rotate keys, audit access, and add encrypted tenant-scoped credentials
before allowing users to manage their own providers. Encrypt sensitive persisted payloads and define
retention controls.
Provider discovery returns names only; it does not reveal whether a key came from an environment
variable or secret manager, and it does not validate upstream availability.

### Observability

HTTP RED metrics use matched route templates and exclude resource IDs and payloads. Workflow labels
are limited to execution mode, lifecycle status, step type, and a bounded result set. Worker labels
contain only recovery outcomes. Protect every scrape endpoint at the network layer in production.
The in-memory registry is process-local: configure one scrape target and distinct port per worker,
or adopt Prometheus multiprocess mode with an explicit lifecycle and cleanup policy.

OpenTelemetry HTTP, workflow, and step spans are available when an OTLP traces endpoint is set. The
engine excludes prompts, inputs, outputs, credentials, and exception messages by design. Protect
collector credentials, configure sampling and retention for the deployment, and review standard
HTTP semantic attributes against the service's data policy. Deferred runs persist only W3C
`traceparent` and `tracestate`; baggage is deliberately excluded, and invalid context is ignored by
workers. Treat trace IDs as operational metadata and apply the same database retention and access
controls as other run metadata. Database-backed gauges expose pending queue depth and
oldest-pending age for backlog and stuck-queue alerts. Worker copies are last observations and can
lag by up to the poll or heartbeat interval; alert from one designated target instead of summing
identical database views. Provider error classification remains future work. Step-attempt counters
expose retry volume, but do not identify individual workflows or providers. Token and cost budgets
count only usage
returned by successful provider calls and may overshoot by one response; they are workflow
guardrails, not billing quotas. Cost rates are operator-managed configuration and must be updated
when the selected model or provider price changes. A budget fails closed when cost is unknown, but
it does not reconcile discounts, cached-token pricing, taxes, credits, or the provider invoice. The
time budget bounds engine work at safe execution boundaries but is not a tenant quota or substitute
for worker isolation.

### Evaluation data

The evaluation runner sends every case through the synchronous API and includes expected and actual
outputs in its JSON report. Treat datasets and reports according to the same data classification and
retention policy as workflow inputs and outputs; do not commit production personal data. Repeated
evaluations create new durable runs and can incur provider usage. The current exact-match scorer is
appropriate for deterministic workflows; semantic, rubric, and model-based graders require a
separate trust and cost policy.

### Provider reliability

Ordered provider fallback plus OpenAI Responses and Anthropic Messages adapters are supported.
Built-in adapters distinguish permanent HTTP/schema failures from transient transport, throttling,
and server failures; an all-permanent chain bypasses remaining step retries. Sanitized errors omit
response bodies and transport messages. Valid `Retry-After` delay-seconds and HTTP dates extend the
normal exponential backoff, using the longest hint across a failed chain. Provider-directed waits
are capped by `PROVIDER_RETRY_AFTER_MAX_SECONDS` (60 seconds by default, at most one hour) and remain
subject to the workflow-wide execution deadline. Size the cap for provider policy and latency SLOs;
a value of zero disables provider-directed extension without disabling configured backoff. Set
`provider_timeout_seconds` on latency-sensitive LLM steps so one slow provider cannot consume
the whole step attempt before fallbacks run. The value applies independently to every provider but
does not extend the enclosing step or workflow deadline. Cancellation is local: a remote provider
may already have accepted work and may still charge for it. For heterogeneous chains, use
`provider_timeout_overrides` to give declared providers different limits; a matching override wins
over the shared value. Keep the enclosing step timeout large enough for the intended sequence of
calls. Add circuit breakers and provider-aware
remote cancellation. Custom providers should raise the normalized error classes; unknown exceptions
remain retryable for backward compatibility. Never retry a non-idempotent tool operation blindly.

Within one active step execution, permanently failed providers are removed from later attempts of a
mixed fallback chain. This reduces repeated authentication or validation failures without creating
cross-run state. The memory is deliberately not durable or process-wide, so a worker restart or a
new run probes the complete chain again. Use an external, shared circuit breaker when deployments
need fleet-wide suppression.

### Database operations

Use managed PostgreSQL, restricted application and migration roles, statement timeouts, encrypted backups, tested restores, migration compatibility checks, and indexes driven by query measurements.

### Supply chain

Pin release dependencies, generate an SBOM, scan container images and dependencies, sign images, protect the default branch, and require CI before merge.

## Threat model highlights

| Risk | v0.1 position | Production control |
|---|---|---|
| Arbitrary code execution | Tools are allowlisted | Sandbox high-risk tools |
| SSRF | No generic HTTP tool | Egress allowlist and URL validation |
| Prompt/data leakage | Fake provider is local | Data classification and redaction |
| Retry duplicates | Run creation is idempotent | Per-tool operation keys and outbox |
| Resource exhaustion | Definition bounds exist | Tenant quotas and worker isolation |
| Cross-tenant access | No tenancy in v0.1 | Tenant-scoped authorization |
