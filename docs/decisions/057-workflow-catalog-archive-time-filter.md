# ADR 057: Bound the workflow catalog by archive time

## Context

The catalog can select the current archive state, but operators investigating a cleanup, incident,
or restoration window cannot ask which workflows were archived during that period. Downloading all
archived definitions and filtering timestamps locally wastes database reads and transfers complete
workflow graphs.

The remaining explicit roadmap item, encrypted tenant credentials, remains blocked by undefined
tenancy, authentication, authorization, credential ownership, and key-rotation contracts.
Archive-time queries improve the current lifecycle API without inventing those security contracts.

## Decision

Add optional `archived_from` and `archived_before` parameters to `GET /v1/workflows`. Define the
same timezone-aware, half-open interval used by other history filters: the lower bound is inclusive
and the upper bound is exclusive. Normalize values to UTC and reject naive, empty, or reversed
ranges with the stable `422 invalid_workflow_archive_time_range` response.

Apply archive-time predicates before keyset traversal and allow them to compose with name, current
archive state, creation-time bounds, limit, and cursor. Because active workflows have a null
`archived_at`, either bound naturally selects archived rows; combining a bound with
`archived=false` returns an empty result. Normalize SQLite comparisons with `julianday`, retain
native PostgreSQL datetime comparisons, and reuse the existing `archived_at` index. No migration is
required.

## Consequences

- Operators can inspect workflows archived only during a known window.
- Adjacent half-open windows neither overlap nor leave a boundary gap.
- Equivalent timezone offsets select the same UTC interval.
- Existing clients remain backward compatible when both bounds are omitted.
- Clients must repeat archive bounds while following a cursor to preserve one logical traversal.
- Archive-time filtering is not deletion, retention, or tenant authorization.
