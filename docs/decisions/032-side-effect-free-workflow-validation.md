# ADR 032: Side-effect-free workflow validation

## Status

Accepted

## Context

Authoring clients and CI checks need to validate workflow drafts before saving them. The creation
endpoint already rejects structurally invalid definitions and unknown provider or tool names without
writing a row, but a valid definition can only be confirmed by creating a workflow. Using creation
as validation produces unwanted records and couples editing flows to persistence cleanup.

## Decision

Add `POST /v1/workflows/validate` with the same `WorkflowCreate` request contract as workflow
creation. Pydantic applies the existing structural and graph checks, then the endpoint reuses the
same provider and tool registry preflight. A valid request returns `{"valid": true}`.

The endpoint has no database dependency and never persists a workflow, contacts a provider, invokes
a tool, or executes a definition. Invalid requests retain the existing `422` response contracts.

## Consequences

- Editors and CI pipelines can validate complete drafts without creating cleanup work.
- Creation and validation share one definition and extension-validation path.
- Validation reflects the API process's configured registries; it is not a provider health check,
  authorization check, execution dry-run, or guarantee that worker configuration will remain equal.
- Configuration can change between validation and creation, so creation keeps its own preflight.
