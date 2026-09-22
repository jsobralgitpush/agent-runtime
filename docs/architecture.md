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
    loop Each step
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
4. The engine resolves input references immediately before each step.
5. Each attempt runs under `asyncio.wait_for`; retry delay grows exponentially.
6. State and telemetry are committed around execution boundaries so a failure remains inspectable.
7. The last step output becomes the workflow output.

## Components

### API

`app/api.py` owns HTTP behavior only: validation, resource lookup, idempotent creation, and response serialization. It does not know provider-specific details.

### Domain and persistence

`Workflow` stores the immutable definition used for new runs. `WorkflowRun` stores lifecycle state and final output. `StepRun` is both the audit trail and the unit of observability, including attempt count, latency, tokens, cost, and error.

### Execution engine

`WorkflowEngine` is an application service rather than a web concern. It accepts a database session, run, and validated definition, so it can later be invoked by a queue worker without changing the workflow semantics.

### Providers and tools

The provider boundary returns a normalized `LLMResult`, preventing provider response shapes from leaking into the engine. The tool boundary is intentionally narrow: an async callable from JSON-compatible input to output. Registries use explicit allowlists, so workflow definitions cannot import arbitrary Python code.

## Reference model

Steps can refer to `${input.key}` or `${steps.step-key.output}`. References are resolved recursively inside dictionaries and lists. Full-value references preserve their JSON type; interpolation into a larger string produces text.

This intentionally avoids an embedded expression language in v0.1. Expression engines add attack surface, nondeterminism, and a second programming model.

## Failure semantics

- Provider and tool calls have bounded timeouts.
- A retry policy controls maximum attempts and exponential backoff.
- A failed step fails the run and prevents later steps from executing.
- Completed prior steps are retained for diagnosis.
- Error strings are recorded; production adapters must redact secrets before raising.

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

## Intentional v0.1 constraint

Execution is synchronous with the request. This makes the correctness model and demo easy to verify. The engine boundary is designed so v0.2 can move execution into workers while the API changes to return `202 Accepted`.

