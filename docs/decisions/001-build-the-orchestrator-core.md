# ADR 001: Build the orchestration core directly

- Status: Accepted
- Date: 2026-09-22

## Context

The project is intended to demonstrate how agent infrastructure works and which reliability concerns appear in production. A large agent framework would provide more features quickly, but would hide the execution model behind framework conventions.

## Decision

Implement the sequential execution core directly using small provider and tool protocols. Use framework-independent workflow definitions and persist normalized execution records.

## Consequences

The implementation is easy to inspect, test, and explain, and providers can be replaced independently. The project owns retry, timeout, reference resolution, and state-transition logic. Advanced DAG planning and ecosystem integrations are deferred.

