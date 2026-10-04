# ADR 014: Enforce workflow cost budgets with operator-managed rates

## Status

Accepted.

## Context

Workflow runs already retain normalized token usage and an optional estimated cost for every LLM
step. The roadmap calls for a cumulative cost budget, but hosted-model prices change independently
of runtime releases and may vary by model, region, contract, or token category. Shipping a static
catalog would make an operational safeguard depend on potentially stale data.

## Decision

Allow a workflow definition to set `max_estimated_cost_usd` above zero and no higher than
1,000,000 USD. OpenAI and Anthropic adapters calculate `estimated_cost_usd` from normalized input
and output token counts plus operator-supplied rates per million tokens. Each provider's input and
output rates must be configured together, must be finite, and must be non-negative. Do not ship
default prices.

After every successful LLM step, add its estimated cost to a decimal accumulator shared by the run.
When consumption exactly reaches the limit, permit tool steps but reject the next LLM call. If a
response takes consumption above the limit, retain the completed step, output, provider, usage, and
cost, then fail the run before another step starts. If a cost-budgeted workflow receives a
successful LLM result without an estimated cost, fail closed rather than silently exempting it.

Apply the behavior identically to synchronous and worker execution. Provider failures and failed
fallback attempts remain uncharged because they do not return normalized usage and cost data.

## Consequences

Deployers can align estimates with the exact models and commercial terms they use without waiting
for a runtime release. Custom and fallback providers participate through the existing
`LLMResult.estimated_cost_usd` contract. Operators must update rates when pricing changes, and a
missing estimate makes a budgeted run fail after the completed call.

Actual cost is known only after a response, so one call can exceed the remaining budget. Estimates
also do not model provider-specific discounts, cached-token tiers, taxes, credits, or invoice
adjustments. The feature is a workflow guardrail and audit aid, not a prepaid quota or billing
reconciliation system.
