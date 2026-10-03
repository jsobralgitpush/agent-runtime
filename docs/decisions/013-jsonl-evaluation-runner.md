# ADR 013: Evaluate workflows from version-controlled JSONL datasets

## Status

Accepted.

## Context

The roadmap includes evaluation datasets, but the runtime has no repeatable way to run known inputs
against a workflow and detect output regressions. A first evaluation slice should reuse production
execution semantics, work without a new persistence model, and produce deterministic results that a
CI job can consume.

## Decision

Provide an `agent-evaluate` CLI that loads up to 1,000 JSON Lines cases. Every line contains a unique
case `id`, an `inputs` object, and an `expected_output` JSON value. Blank lines are ignored; malformed
cases and duplicate IDs reject the dataset before provider calls begin.

Submit cases sequentially to the existing synchronous workflow-run endpoint. Compare the completed
run's final output to `expected_output` using exact JSON equality without string coercion. Emit one
JSON summary containing counts, run IDs, expected values, actual values, and failure details. Exit
with status 0 when all cases pass, 1 when any expectation fails, and 2 for dataset, transport, or API
contract errors.

Do not introduce a separate evaluation execution path or database model. Evaluation runs retain the
same state transitions, telemetry, budgets, provider behavior, and durable audit history as normal
runs.

## Consequences

Teams can review datasets in source control and use the runner as a regression gate without adding
infrastructure. Exact equality is intentionally narrow and is most useful for deterministic tools,
structured outputs, and stable fake providers. Natural-language quality needs separate semantic,
rubric, or model-based graders with explicit trust, variance, and cost policies. The report contains
workflow outputs, and every invocation creates new runs and may incur provider cost, so datasets and
reports require appropriate data handling.
