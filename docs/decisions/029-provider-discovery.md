# ADR 029: Expose configured provider names

## Status

Accepted

## Context

Workflow definitions refer to providers by name, but clients currently learn which providers are
available only from deployment documentation or from a failed run. Optional adapters appear only
when their process-level credentials are configured, so the valid set can differ by deployment.

Returning full adapter configuration would expose operational details and could leak credentials.
Calling every upstream service during discovery would also make a local metadata request slow and
fragile.

## Decision

Add `GET /v1/providers`, returning a sorted list of objects containing provider names only. Source
the names from the same `ProviderRegistry` construction used by workflow execution. Always include
the deterministic `fake` provider; include optional built-in adapters only when configured.

Treat discovery as local configuration metadata, not readiness. Do not call providers, expose
models or endpoints, or return credential presence and origin beyond the fact that an adapter is
available.

## Consequences

Clients can discover valid provider identifiers before constructing workflows, and the contract can
gain non-sensitive capabilities later without changing from a list of strings to objects. Sorting
makes responses deterministic.

A listed provider may still be unavailable, misconfigured upstream, rate-limited, or different from
the provider set in another API or worker process. Deployments must keep runtime configuration
consistent and use actual workflow execution plus monitoring to assess provider health.
