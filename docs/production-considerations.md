# Production considerations

v0.1 demonstrates production concerns but is not presented as a finished multi-tenant service. This document distinguishes implemented safeguards from the next operational layer.

## Already implemented

- Database uniqueness is the source of truth for request idempotency.
- Provider and tool implementations are allowlisted, not dynamically imported.
- Workflow size, identifiers, retries, and timeouts have validation bounds.
- Workflows can bound cumulative execution time and reported LLM token usage.
- Each execution boundary records status, attempts, latency, output, usage, cost, and errors.
- `/v1/metrics` exposes low-cardinality HTTP request, error, latency, and concurrency metrics.
- Optional OTLP/HTTP tracing correlates API requests with workflow and step execution.
- JSONL evaluation datasets can exercise persisted workflows with exact expected outputs.
- The container uses a non-root user.
- CI has read-only repository permissions and a frozen dependency installation.
- The fake provider enables deterministic tests without sending data externally.

## Required before external traffic

### Queueing and recovery

Deferred execution uses database-backed workers with renewable leases and heartbeats. Expired runs
are terminalized as failed without replay, preventing them from remaining ambiguously `running`.
Before production use, add explicit retry/resume operations according to persisted step state and
a documented at-least-once strategy, especially for side effects.

### Side-effect idempotency

Run creation idempotency does not make arbitrary tools idempotent. Pass a stable operation key (`run_id:step_key`) to side-effecting tools and require downstream idempotency or implement an outbox.

### Authentication and tenancy

Add authentication, tenant ownership columns, row-level authorization, rate limits, payload limits, and audit events. Every query must be scoped by tenant.

### Secret handling

The OpenAI and Anthropic adapters accept environment-injected API keys and never persist them in
workflow data. For production, source provider credentials from a cloud secret manager rather than
a checked-in `.env` file. Rotate keys, audit access, and add encrypted tenant-scoped credentials
before allowing users to manage their own providers. Encrypt sensitive persisted payloads and define
retention controls.

### Observability

HTTP RED metrics use matched route templates and exclude resource IDs and payloads. Protect the
scrape endpoint at the network layer in production. The in-memory registry is process-local; a
multi-process deployment needs Prometheus multiprocess mode or one scrape target per process.

OpenTelemetry HTTP, workflow, and step spans are available when an OTLP traces endpoint is set. The
engine excludes prompts, inputs, outputs, credentials, and exception messages by design. Protect
collector credentials, configure sampling and retention for the deployment, and review standard
HTTP semantic attributes against the service's data policy. Add workflow/worker metrics, retry
counts, stuck-run alerts, cost budgets, and provider error classification. Token budgets count only
usage returned by successful provider calls and may overshoot by one response; they are a workflow
guardrail, not a billing quota. The time budget bounds engine work at safe execution boundaries but
is not a tenant quota or substitute for worker isolation.

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
