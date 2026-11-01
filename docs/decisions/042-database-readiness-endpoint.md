# ADR 042: Database-backed readiness endpoint

## Context

The API exposes `/v1/health`, but that endpoint only proves the process can serve HTTP. Every useful
workflow operation depends on persistence, so an orchestrator can route traffic to a live process
whose database connection is unavailable. Reusing liveness for dependency checks can also create
restart loops during a database outage.

## Decision

Keep `GET /v1/health` dependency-free and add `GET /v1/ready`. Readiness performs one minimal
`SELECT 1` database round trip through the normal session dependency. Return `{"status":"ready"}`
on success. Catch SQLAlchemy database errors and return HTTP `503` with only the stable
`database_unavailable` code; do not expose connection strings, driver messages, or SQL details.

Do not contact providers, execute tools, inspect worker processes, or mutate state. Those checks are
more expensive, can have side effects, and represent separate availability domains.

## Consequences

- Orchestrators can distinguish process liveness from the ability to serve persistence-backed API
  traffic.
- A database outage removes the API from traffic without forcing a process restart.
- Probe behavior is deterministic and does not leak database diagnostics to clients.
- Readiness does not prove migrations are current, workers are consuming runs, providers are
  reachable, or tenant-specific configuration is valid; operators must monitor those separately.
- Deployments should tune probe cadence and database connection-pool capacity together to avoid
  probe traffic becoming material load.
