# ADR 015: Plan explicit step dependencies deterministically

## Status

Accepted.

## Context

Workflow execution has been restricted to declaration order. This keeps the initial engine easy to
inspect, but it couples data dependencies to file layout and prevents a workflow from expressing a
graph directly. The v0.4 roadmap calls for DAG execution and parallel branches. Parallel execution
also introduces database-session ownership, sibling cancellation, and shared-budget coordination,
so dependency semantics should be established independently first.

## Decision

Allow each step to declare up to 49 predecessor keys in `depends_on`. Validate the complete graph
when the workflow definition is created: dependency keys must be valid and unique, every target must
exist, a step cannot depend on itself, and the graph cannot contain a cycle.

When a definition uses explicit dependencies, require exactly one terminal step. Its output becomes
the workflow output. This makes completion semantics unambiguous and requires branches to converge
through an explicit aggregation step. Definitions without dependencies retain their existing
behavior and use the final declared step as the output.

Execute dependency-ready steps in a stable topological order, using declaration order as the
tie-breaker. Persist `StepRun.position` as execution position. Continue to execute one step at a time
in both synchronous and worker modes; do not introduce parallel database-session use in this
change.

## Consequences

Authors can place steps in any declaration order and express branches and convergence explicitly.
Invalid graphs fail during API validation rather than partway through a run, and identical
definitions produce identical plans. Existing linear definitions remain compatible.

Independent branches do not yet reduce latency. A later parallel executor must give each concurrent
task safe persistence boundaries, cancel or contain sibling work after failures, coordinate leases,
and update shared time, token, and cost budgets atomically. Workflows needing multiple final values
must currently add a terminal aggregation tool step.
