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
- The container uses a non-root user.
- CI has read-only repository permissions and a frozen dependency installation.
- The fake provider enables deterministic tests without sending data externally.

## Required before external traffic

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

### Secret handling

The OpenAI and Anthropic adapters accept environment-injected API keys and never persist them in
workflow data. For production, source provider credentials from a cloud secret manager rather than
a checked-in `.env` file. Rotate keys, audit access, and add encrypted tenant-scoped credentials
before allowing users to manage their own providers. Encrypt sensitive persisted payloads and define
retention controls.

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

Ordered provider fallback plus OpenAI Responses and Anthropic Messages adapters are supported. Add
circuit breakers, rate-limit-aware retry hints, request cancellation, per-provider timeout policy,
and normalized error classes. Retry only failures known to be transient; never retry a
non-idempotent operation blindly.

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
