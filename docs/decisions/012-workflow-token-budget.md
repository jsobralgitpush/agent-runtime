# ADR 012: Bound cumulative workflow token usage

## Status

Accepted.

## Context

The roadmap includes token and cost budgets. Every current provider normalizes prompt and completion
usage into `LLMResult`, while hosted providers deliberately leave estimated cost unknown because the
runtime has no durable model-price catalog. A token limit can therefore be consistent today without
inventing cost data or pricing policy.

## Decision

Allow a workflow definition to set `max_total_tokens` above zero and no higher than 10,000,000.
After each successful LLM step, add its reported prompt and completion tokens to one cumulative
counter shared by the entire run. Tool steps consume no tokens.

When consumption exactly reaches the limit, allow later tool steps but reject the next LLM step
before creating its step record or calling a provider. When a response takes consumption above the
limit, preserve that completed step, output, provider, and usage, then fail the run with
`WorkflowTokenBudgetExceededError` before starting another step. Apply the same semantics in
synchronous and worker execution.

Count only normalized usage from successful provider results. Do not estimate usage for failed
provider or fallback attempts, because their exceptions do not carry a trustworthy usage contract.

## Consequences

Workflow authors can stop additional model calls after a known token envelope while retaining a
complete audit trail. A single response can exceed the remaining budget because actual usage is
available only after completion. The feature is therefore a workflow guardrail, not an exact prepaid
quota or billing control. A future provider-neutral request limit could reduce overshoot, but would
need explicit input/output allocation semantics. Cost budgets remain separate until pricing and
unknown-cost behavior are defined.
