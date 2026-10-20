# ADR 030: Expose allowlisted tool names

## Status

Accepted

## Context

Tool steps refer to async callables by registry name. The runtime documents its built-in tools, but
clients cannot inspect the active allowlist through the API and may submit a workflow that fails only
when execution tries to resolve an unknown tool. ADR 029 provides a names-only discovery contract
for providers; tools need the same safe, deterministic affordance.

Callable objects and implementation details are not serializable API contracts. Discovery must also
remain separate from execution so listing tools cannot trigger side effects.

## Decision

Add `GET /v1/tools`, returning a sorted list of objects containing names from the runtime's default
`ToolRegistry`. Add a read-only `names()` view to the registry. Return no callable representation,
input schema, source location, or execution result, and do not invoke tools during discovery.

Treat the endpoint as capability discovery rather than authorization or validation. Future
multi-tenant deployments must protect it with the same API authorization boundary as workflow
operations.

## Consequences

Clients can enumerate the built-in allowlist before composing a workflow, and deterministic ordering
makes responses stable for generated UIs and tests. The object response shape can later carry safe,
explicit metadata without breaking a string-list contract.

Embedded applications that inject a custom `ToolRegistry` directly into `WorkflowEngine` do not
automatically change this HTTP endpoint. Exposing deployment-specific custom tools will require an
application-level registry dependency and an authorization decision. A listed name also does not
prove that arbitrary inputs are valid for that tool.
