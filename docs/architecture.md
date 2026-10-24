# Architecture

## Goals

Agent Runtime v0.1 provides the smallest end-to-end slice of a durable AI workflow system: validated workflow definitions, sequential execution, persistent run history, failure policies, and telemetry. It favors explicit interfaces and inspectable code over a broad feature set.

## Execution lifecycle

```mermaid
sequenceDiagram
    participant Client
    participant API
    participant Store as PostgreSQL
    participant Engine
    participant Target as Provider or tool
    Client->>API: POST workflow run
    API->>Store: Insert pending run
    API->>Engine: Execute definition
    loop Each executable step
        Engine->>Store: Insert running step
        Engine->>Target: Execute with timeout
        Target-->>Engine: Output and usage
        Engine->>Store: Persist result and telemetry
    end
    Engine->>Store: Complete run
    API-->>Client: Run with step history
```

1. The API validates a workflow definition with Pydantic before persisting it.
2. A client creates a run with inputs and an optional `Idempotency-Key` header.
3. A unique database constraint guarantees one run per workflow and idempotency key, including races.
4. The engine creates a stable topological plan and resolves input references immediately before each step.
5. Each attempt runs under `asyncio.wait_for`; retry delay grows exponentially.
6. State and telemetry are committed around execution boundaries so a failure remains inspectable.
7. The terminal step output becomes the workflow output.

An approval step replaces the target call with a durable boundary: the engine stores its resolved
review context, changes the step and run to `waiting`, and returns. A later approve decision is
stored as the step output before the same engine reconstructs prior outputs and resumes. Rejection
terminalizes the step and run without executing descendants.

Approval can resume immediately in the API process or, with `Prefer: respond-async`, transition the
run back to `pending` and return `202 Accepted`. The normal worker claim path then leases the run and
invokes the same resume-aware engine. Queueing the continuation is an explicit client choice so the
existing synchronous API remains backward compatible.

## Components

### API

`app/api.py` owns HTTP behavior only: validation, resource lookup, idempotent creation, and response serialization. It does not know provider-specific details.

Workflow run history uses keyset pagination ordered by `(created_at, id)` descending. The last run
ID in a non-final page is an opaque cursor: the API reloads that row, verifies that it belongs to
the requested workflow, then queries strictly older rows. A composite database index on
`(workflow_id, created_at, id)` supports the bounded query without offset scans. Each page eagerly
loads step history so it has the same response contract as direct run lookup.
An optional exact lifecycle-status filter is applied before the keyset boundary. The unfiltered
query uses `(workflow_id, created_at, id)`, while filtered history uses the covering prefix
`(workflow_id, status, created_at, id)`. The cursor itself remains a workflow-scoped ordering
boundary rather than encoding mutable status, so a run transition cannot make its cursor unreadable.

An ASGI middleware records Prometheus request counts, duration, and current concurrency. Labels use
the matched route template rather than the request path, so workflow and run IDs cannot create an
unbounded time-series set. The scrape endpoint is excluded from its own metrics.

The engine also records invocation counts and duration by execution mode and resulting status, and
step attempts by step type and result. Worker processes add claim, active-run, and lease-recovery
metrics to the same process-local registry. Since API and worker memory is isolated, a worker can
optionally serve that registry on its own `WORKER_METRICS_PORT`; each process remains an independent
Prometheus scrape target. Queue depth and oldest-pending age are database-backed gauges: the API
refreshes them on each scrape, while metrics-enabled workers refresh them after claims, idle polls,
and active-run heartbeats. Metrics never label workflow IDs, run IDs, step keys, worker IDs, provider
names, prompts, outputs, or exception messages.

When `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` is set, FastAPI instrumentation creates HTTP server spans
and exports them in batches over OTLP/HTTP. Synchronous `workflow.run` spans inherit that request
context. For a deferred run, the API stores only the current W3C `traceparent` and `tracestate`; the
worker extracts that context before creating `workflow.run`, preserving the trace across the
database queue. A deferred approval continuation replaces the stored context so it follows the
decision request. Every `workflow.step` span is a child of its run span. The engine adds identifiers,
execution mode, status, step type, attempt count, and selected provider, but never adds inputs,
prompts, outputs, credentials, baggage, or error messages. Invalid or missing persisted context is
ignored. With no endpoint, the runtime does not install a tracer provider, exporter, or background
processor.

### Domain and persistence

`Workflow` stores the immutable definition used for new runs. `WorkflowRun` stores lifecycle state and final output. `StepRun` is both the audit trail and the unit of observability, including attempt count, latency, tokens, cost, and error.

### Execution engine

`WorkflowEngine` is an application service rather than a web concern. It accepts a database session, run, and validated definition, so it can later be invoked by a queue worker without changing the workflow semantics.

Definitions can declare dependency edges between steps. Validation rejects unknown, duplicate,
self-referential, and cyclic edges, and dependency-aware definitions must converge on one terminal
step that supplies the run output. The planner uses declaration order as a deterministic tie-breaker
for simultaneously ready nodes. By default the engine remains serial; `max_parallel_steps` can opt
into bounded waves of concurrent provider and tool calls.

Parallel tasks never access the SQLAlchemy session. The engine persists the wave's running records,
launches external calls, waits for every sibling, verifies an active worker lease when applicable,
and commits all terminal step states through the owning coroutine. A branch failure prevents later
waves but does not discard successful sibling telemetry. Wall-clock limits share one deadline;
cumulative token and cost limits are reconciled after a wave and may therefore overshoot by the
usage of all calls already in flight.

Approval steps are exclusive waves in the dependency-ready plan. Declaration order remains the
tie-breaker: ready work before a gate can complete, the gate then pauses the run, and ready work
after it cannot start early. When execution resumes, the engine rebuilds references and cumulative
token/cost accounting from completed `StepRun` records. The `(run_id, step_key)` uniqueness
constraint also prevents a resume path from duplicating a completed step. Human waiting time is
outside the active execution deadline; `max_runtime_seconds` starts a fresh active segment on each
resume.

### Evaluation runner

`agent-evaluate` loads version-controlled JSONL cases and submits each case to the existing
synchronous run endpoint. It compares the final output with `expected_output` using exact JSON
equality and emits a machine-readable summary with a CI-compatible exit status. Evaluation does not
have a privileged execution path: each case creates the same durable run history, telemetry,
budgets, and provider calls as any other API request. Cases execute sequentially to keep ordering and
resource use predictable.

### Providers and tools

The provider boundary returns a normalized `LLMResult`, preventing provider response shapes from leaking into the engine. The tool boundary is intentionally narrow: an async callable from JSON-compatible input to output. Registries use explicit allowlists, so workflow definitions cannot import arbitrary Python code.

`GET /v1/providers` exposes the registry's sorted provider names so clients can build valid workflow
definitions against the current runtime configuration. The response deliberately omits models,
adapter objects, endpoints, and credentials. It is discovery rather than a health probe: a listed
provider is configured, but the endpoint does not call its remote service.

`GET /v1/tools` provides the corresponding sorted view of the built-in tool allowlist. It returns
names only and never invokes or serializes tool callables. Discovery communicates what the runtime
can resolve; it does not grant permission to execute a tool or validate a workflow definition.

Workflow creation uses those same API-process registries as a preflight boundary. It aggregates
unknown primary providers, fallback providers, and tools into one deterministic `422` response
before persistence. This catches configuration errors early without contacting any provider or
executing a tool. Workers still validate by resolution at execution time, so deployments must keep
API and worker registry configuration aligned.

`POST /v1/workflows/validate` accepts the same contract as creation and applies both Pydantic
structural checks and registry preflight without opening a database session or writing a workflow.
Success returns only `{"valid": true}`. The endpoint never contacts providers or invokes tools, so
it validates a definition against the API process rather than simulating its execution.

LLM steps may declare an ordered fallback chain. The engine tries the primary provider followed by
each fallback within the existing step timeout and retry boundary. Only the successful provider is
persisted on `StepRun`; failed provider attempts emit structured logs without provider exception
messages, reducing the chance of logging sensitive response data. Built-in adapters normalize
transport failures and retryable HTTP statuses (`408`, `409`, `425`, `429`, and `5xx`) as transient;
other HTTP failures and malformed successful responses are permanent. The engine always tries the
full fallback chain, then repeats it only when at least one failure was transient. Unknown failures
from custom providers remain retryable for backward compatibility. Transient provider errors can
carry a parsed `Retry-After` hint. Before repeating the chain, the engine takes the largest hint,
never shortens configured exponential backoff, and limits provider-directed waiting with
`PROVIDER_RETRY_AFTER_MAX_SECONDS`. The workflow execution deadline remains the final bound.

An LLM step can opt into `provider_timeout_seconds` to place the same independent wall-clock limit
on every call in its provider chain. Exceeding that limit cancels the local coroutine and becomes a
sanitized, transient `ProviderTimeoutError`, allowing fallback and step retry policies to proceed.
The per-step attempt timeout still wraps the complete chain, and the workflow deadline still wraps
the complete execution, so independent call limits never extend either outer budget.
Provider-specific values can be declared with `provider_timeout_overrides`. Override keys must name
providers already present in that step's chain, and a matching override takes precedence over the
shared `provider_timeout_seconds` value. Providers without an override inherit the shared value;
when neither is configured, they remain governed only by the outer step and workflow limits.

The retry loop also retains permanent provider failures for the lifetime of one active step
execution. If a mixed chain must retry because at least one failure was transient, providers that
already returned a permanent failure are skipped on later attempts. This state is step-local and
in-memory; independent steps, new runs, and recovered processes begin with the full configured
chain.

When `OPENAI_API_KEY` is configured, the provider registry also exposes `openai`. Its adapter calls
the Responses API with storage disabled, passes run and step IDs as metadata, and normalizes text and
token usage into the same `LLMResult` used by the fake provider. Credentials remain process-level
configuration and are not part of persisted workflow definitions.

OpenAI and Anthropic cost estimates use operator-supplied input and output rates per million tokens.
The rates are paired process configuration rather than persisted workflow data or a hardcoded price
catalog. This keeps model selection and pricing policy with the deployer while preserving a common
`estimated_cost_usd` field for engine enforcement and telemetry.

The `anthropic` adapter is registered under the same rule when `ANTHROPIC_API_KEY` is present. It
calls the Messages API with a bounded output-token budget and normalizes text blocks and token usage.
Non-text blocks such as model thinking remain outside the runtime's provider-neutral result shape.

## Reference model

Steps can refer to `${input.key}`, `${steps.step-key.output}`, `${runtime.run_id}`,
`${runtime.step_key}`, or `${runtime.operation_key}`. Runtime values are derived by the engine for
the current step; the operation key is `run_id:step_key`. References are resolved recursively inside
dictionaries and lists before the first attempt, so every retry and safe resume uses the same
resolved operation key. Full-value references preserve their JSON type; interpolation into a larger
string produces text.

This intentionally avoids an embedded expression language in v0.1. Expression engines add attack surface, nondeterminism, and a second programming model.

## Failure semantics

- Provider and tool calls have bounded timeouts.
- Invalid or ambiguous dependency graphs are rejected before a workflow is persisted.
- A workflow may define a cumulative wall-clock budget across calls and retry backoffs.
- A workflow may bound cumulative tokens reported by successful LLM calls.
- A workflow may bound cumulative estimated cost reported by successful LLM calls.
- A retry policy controls maximum attempts and exponential backoff.
- A failed step fails the run and prevents later steps from executing.
- A rejected approval fails the run and prevents later steps from executing.
- An approval decision is accepted only while exactly one approval step is waiting.
- Completed prior steps are retained for diagnosis.
- Error strings are recorded; production adapters must redact secrets before raising.

The workflow budget and step timeout are independent: the smaller remaining duration controls each
attempt. Exhausting the workflow budget cancels the active call, marks that step and the run failed
with `WorkflowTimeBudgetExceededError`, and prevents later steps from starting. The engine checks the
deadline at safe boundaries; it allows an active database transaction to finish, and that elapsed
time reduces the budget available to the next call.

The token budget sums prompt and completion usage after each successful LLM response. Reaching the
limit prevents another LLM call but does not block tool-only work. A response can exceed the
remaining budget because usage is not known in advance; in that case the completed step and its
usage are retained, the run fails with `WorkflowTokenBudgetExceededError`, and later steps do not
start. Provider failures that do not return normalized usage cannot be charged.

The cost budget mirrors those boundaries using each successful step's normalized
`estimated_cost_usd`. Reaching the exact limit permits tool steps but prevents another LLM call; a
single response may overshoot and remains recorded before the run fails with
`WorkflowCostBudgetExceededError`. If a cost-budgeted workflow receives a successful response with
no cost estimate, it fails closed with `WorkflowCostUnavailableError`. This prevents silently
bypassing the guardrail when a custom, fallback, or unpriced provider is selected.

## Data model

```mermaid
erDiagram
    WORKFLOW ||--o{ WORKFLOW_RUN : has
    WORKFLOW_RUN ||--o{ STEP_RUN : records
    WORKFLOW {
        uuid id PK
        json definition
    }
    WORKFLOW_RUN {
        uuid id PK
        string idempotency_key UK
        string status
        json inputs
        json output
    }
    STEP_RUN {
        uuid id PK
        string step_key UK
        string status
        int attempts
        float latency_ms
        int prompt_tokens
        float estimated_cost_usd
    }
```

## Execution modes

Execution remains synchronous by default, preserving the simple local demo. Clients can send
`Prefer: respond-async` to persist a pending run and receive `202 Accepted`; an `agent-worker`
process claims the oldest pending run with `FOR UPDATE SKIP LOCKED` and invokes the same engine.
The database is the initial queue so run creation and claim state share one transaction boundary.
Each claim stores a worker ID, heartbeat timestamp, and lease deadline. A separate database session
renews the lease during execution so provider calls do not block heartbeats.

Before claiming new work, a worker locks expired runs in bounded batches and marks each run plus
its incomplete steps as failed by default. If every interrupted provider or tool step explicitly
declares `replay_safe`, the worker instead returns the run and incomplete step records to `pending`.
The next worker preserves completed checkpoints, reuses those step records, and increments their
attempt counts before calling the targets again. An expired lease with no started step is also safe
to requeue because no external call crossed the execution boundary.

Clearing ownership fences out the previous worker. Before persisting a step result, every leased
worker locks the run row and verifies that its ownership and deadline are still valid, preventing a
late result from overwriting recovery. Replay safety is a workflow-author assertion rather than a
property the runtime can inspect. Side-effecting steps should combine it with the stable
`${runtime.operation_key}` and downstream idempotency enforcement.
