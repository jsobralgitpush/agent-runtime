# ADR 031: Validate workflow extensions before persistence

## Status

Accepted

## Context

Workflow definitions name providers, fallback providers, and tools. Their structural contracts are
validated at creation, but registry membership is currently checked only when a run executes the
step. A typo can therefore persist successfully, enter the worker queue, and fail after unrelated
earlier steps have already consumed time or provider budget.

ADRs 029 and 030 expose deterministic, names-only registry views. The API can use the same local
views for an early compatibility check without calling providers or executing tools.

## Decision

Before persisting a workflow, compare every LLM primary/fallback name and tool name with the current
API process's `ProviderRegistry` and `ToolRegistry`. Aggregate all unknown names, sort them, and
return HTTP `422` with the stable code `unknown_workflow_extensions`. Include separate
`unknown_providers` and `unknown_tools` arrays and do not persist the workflow.

Keep execution-time registry resolution as the final authority. Do not make remote health calls and
do not treat successful preflight as proof that a provider is healthy or that API and worker
processes share identical configuration.

## Consequences

Typos and missing optional-provider configuration fail before a workflow can create durable invalid
state or consume execution resources. Aggregating errors lets clients correct the full definition in
one pass, and deterministic ordering makes the response suitable for generated forms and tests.

The check is local to the API process. Configuration drift can still let a definition pass creation
and fail on a worker, while an API missing an adapter can reject a definition that a worker could
run. Custom embedded registries are not part of the default HTTP application until the runtime gains
an application-level registry dependency. This is compatibility preflight, not authorization,
provider readiness, or input-schema validation for tools.
