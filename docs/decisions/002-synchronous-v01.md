# ADR 002: Execute v0.1 synchronously behind a worker-ready boundary

- Status: Accepted
- Date: 2026-09-22

## Context

Durable workers are necessary for long-running workflows, but introducing a broker, worker leases, recovery, and delivery semantics at the start would obscure the core execution behavior and lengthen the local setup.

## Decision

Execute a run inside the API request for v0.1. Keep `WorkflowEngine` independent of FastAPI so a later worker can call the same application service. Persist state before, during, and after execution.

## Consequences

The complete system is locally demonstrable and deterministic. Requests remain open during execution and cannot recover automatically from process death. v0.2 must add queue-backed execution before the service handles slow or high-volume workloads.

