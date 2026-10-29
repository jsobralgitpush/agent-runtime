# ADR 039: Workflow run status summary

## Context

The history endpoint can filter and paginate complete run records, including every step's input,
output, error, and telemetry. Operational clients that only need lifecycle totals currently have to
walk those pages and transfer substantially more data than the question requires.

## Decision

Add `GET /v1/workflows/{workflow_id}/runs/summary`. Return the workflow ID, total run count, and an
explicit count for every `RunStatus`, including zeroes for states with no rows. Compute the response
with one workflow-scoped `GROUP BY status` query. Reuse the existing
`(workflow_id, status, created_at, id)` index and do not load run payloads or step relationships.

Return `404` when the workflow does not exist, matching detailed history. Keep summary aggregation
separate from history filters: this endpoint describes all runs for the workflow at query time.

## Consequences

- Dashboards and operators can inspect lifecycle distribution with bounded response size.
- The response has a stable shape even when a workflow has no runs or no runs in a specific state.
- No schema migration is required.
- Counts can change immediately after the query and are not a transactional snapshot shared with a
  later history request.
- Multi-tenant deployments must authorize ownership before returning the aggregate. Counts are
  operational metadata, not safe public information or Prometheus labels keyed by workflow ID.
