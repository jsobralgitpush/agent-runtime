import asyncio
import json
import logging
import re
import time
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode, Tracer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import RunStatus, StepRun, WorkflowRun
from app.providers import LLMResult, ProviderRegistry, build_provider_registry
from app.schemas import WorkflowDefinition, WorkflowStep
from app.tools import ToolRegistry

logger = logging.getLogger(__name__)
REFERENCE = re.compile(r"\$\{(input|steps)\.([a-zA-Z0-9_-]+)(?:\.output)?\}")


class WorkerLeaseLostError(RuntimeError):
    pass


class AllProvidersFailedError(RuntimeError):
    pass


class WorkflowTimeBudgetExceededError(TimeoutError):
    pass


class WorkflowTokenBudgetExceededError(RuntimeError):
    pass


def token_budget_exceeded(
    consumed_tokens: int, max_total_tokens: int
) -> WorkflowTokenBudgetExceededError:
    return WorkflowTokenBudgetExceededError(
        f"Workflow token budget of {max_total_tokens} exhausted after "
        f"consuming {consumed_tokens} tokens"
    )


def remaining_time_budget(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    remaining = deadline - time.perf_counter()
    if remaining <= 0:
        raise WorkflowTimeBudgetExceededError("Workflow execution time budget exhausted")
    return remaining


async def wait_for_retry(delay: float, deadline: float | None) -> None:
    remaining = remaining_time_budget(deadline)
    if remaining is None:
        await asyncio.sleep(delay)
        return
    await asyncio.sleep(min(delay, remaining))
    if delay >= remaining:
        raise WorkflowTimeBudgetExceededError("Workflow execution time budget exhausted")


async def assert_active_lease(
    session: AsyncSession,
    run_id: str,
    lease_owner: str,
    *,
    now: datetime | None = None,
) -> None:
    checked_at = now or datetime.now(UTC)
    active_run_id = await session.scalar(
        select(WorkflowRun.id)
        .where(
            WorkflowRun.id == run_id,
            WorkflowRun.status == RunStatus.running,
            WorkflowRun.lease_owner == lease_owner,
            WorkflowRun.lease_expires_at.is_not(None),
            WorkflowRun.lease_expires_at > checked_at,
        )
        .with_for_update()
    )
    if active_run_id is None:
        raise WorkerLeaseLostError(f"Worker {lease_owner} no longer owns run {run_id}")


def _lookup_reference(match: re.Match[str], inputs: dict[str, Any], outputs: dict[str, Any]) -> Any:
    namespace, key = match.group(1), match.group(2)
    source = inputs if namespace == "input" else outputs
    if key not in source:
        raise ValueError(f"Unknown workflow reference: {match.group(0)}")
    return source[key]


def resolve_references(value: Any, inputs: dict[str, Any], outputs: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        return {key: resolve_references(item, inputs, outputs) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_references(item, inputs, outputs) for item in value]
    if not isinstance(value, str):
        return value
    full_match = REFERENCE.fullmatch(value)
    if full_match:
        return _lookup_reference(full_match, inputs, outputs)

    def replace(match: re.Match[str]) -> str:
        resolved = _lookup_reference(match, inputs, outputs)
        return resolved if isinstance(resolved, str) else json.dumps(resolved, sort_keys=True)

    return REFERENCE.sub(replace, value)


class WorkflowEngine:
    def __init__(
        self,
        settings: Settings,
        providers: ProviderRegistry | None = None,
        tools: ToolRegistry | None = None,
        tracer: Tracer | None = None,
    ) -> None:
        self.settings = settings
        self.providers = providers or build_provider_registry(settings)
        self.tools = tools or ToolRegistry()
        self.tracer = tracer or trace.get_tracer(__name__)

    async def execute(
        self,
        session: AsyncSession,
        run: WorkflowRun,
        definition: WorkflowDefinition,
        *,
        lease_owner: str | None = None,
    ) -> WorkflowRun:
        attributes = {
            "agent_runtime.run.id": run.id,
            "agent_runtime.workflow.id": run.workflow_id,
            "agent_runtime.run.mode": "worker" if lease_owner is not None else "synchronous",
        }
        with self.tracer.start_as_current_span(
            "workflow.run",
            attributes=attributes,
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                result = await self._execute_workflow(
                    session,
                    run,
                    definition,
                    lease_owner=lease_owner,
                )
            except Exception as exc:
                span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                raise
            span.set_attribute("agent_runtime.run.status", result.status.value)
            if result.status == RunStatus.failed:
                span.set_status(Status(StatusCode.ERROR))
            return result

    async def _execute_workflow(
        self,
        session: AsyncSession,
        run: WorkflowRun,
        definition: WorkflowDefinition,
        *,
        lease_owner: str | None = None,
    ) -> WorkflowRun:
        outputs: dict[str, Any] = {}
        consumed_tokens = 0
        lease_lost = False
        run.status = RunStatus.running
        run.started_at = datetime.now(UTC)
        await session.commit()
        logger.info(
            "workflow_run_started", extra={"run_id": run.id, "workflow_id": run.workflow_id}
        )
        deadline = (
            time.perf_counter() + definition.max_runtime_seconds
            if definition.max_runtime_seconds is not None
            else None
        )

        try:
            for position, step in enumerate(definition.steps):
                remaining_time_budget(deadline)
                if (
                    step.type == "llm"
                    and definition.max_total_tokens is not None
                    and consumed_tokens >= definition.max_total_tokens
                ):
                    raise token_budget_exceeded(
                        consumed_tokens,
                        definition.max_total_tokens,
                    )
                step_run = StepRun(
                    run_id=run.id,
                    step_key=step.key,
                    position=position,
                    step_type=step.type,
                    status=RunStatus.pending,
                )
                session.add(step_run)
                await session.flush()
                output = await self._execute_step(
                    session,
                    run,
                    step,
                    step_run,
                    outputs,
                    lease_owner,
                    deadline,
                )
                consumed_tokens += (step_run.prompt_tokens or 0) + (step_run.completion_tokens or 0)
                if (
                    definition.max_total_tokens is not None
                    and consumed_tokens > definition.max_total_tokens
                ):
                    raise token_budget_exceeded(
                        consumed_tokens,
                        definition.max_total_tokens,
                    )
                outputs[step.key] = output
            run.status = RunStatus.completed
            run.output = outputs[definition.steps[-1].key]
        except WorkerLeaseLostError:
            lease_lost = True
            await session.rollback()
            logger.warning("workflow_run_lease_lost", extra={"run_id": run.id})
        except Exception as exc:
            run.status = RunStatus.failed
            run.error = f"{type(exc).__name__}: {exc}"
            logger.exception("workflow_run_failed", extra={"run_id": run.id})
        finally:
            if lease_lost:
                await session.refresh(run)
            else:
                run.completed_at = datetime.now(UTC)
                await session.commit()
            await session.refresh(run, attribute_names=["steps"])

        logger.info("workflow_run_finished", extra={"run_id": run.id, "status": run.status.value})
        return run

    async def _execute_step(
        self,
        session: AsyncSession,
        run: WorkflowRun,
        step: WorkflowStep,
        step_run: StepRun,
        outputs: dict[str, Any],
        lease_owner: str | None,
        deadline: float | None,
    ) -> Any:
        attributes = {
            "agent_runtime.run.id": run.id,
            "agent_runtime.step.key": step.key,
            "agent_runtime.step.type": step.type,
        }
        with self.tracer.start_as_current_span(
            "workflow.step",
            attributes=attributes,
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                output = await self._execute_step_inner(
                    session,
                    run,
                    step,
                    step_run,
                    outputs,
                    lease_owner,
                    deadline,
                )
            except Exception as exc:
                span.set_attribute("agent_runtime.step.attempts", step_run.attempts)
                span.set_attribute("agent_runtime.step.status", step_run.status.value)
                span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                raise
            span.set_attribute("agent_runtime.step.attempts", step_run.attempts)
            span.set_attribute("agent_runtime.step.status", step_run.status.value)
            if step_run.provider is not None:
                span.set_attribute("agent_runtime.step.provider", step_run.provider)
            return output

    async def _execute_step_inner(
        self,
        session: AsyncSession,
        run: WorkflowRun,
        step: WorkflowStep,
        step_run: StepRun,
        outputs: dict[str, Any],
        lease_owner: str | None,
        deadline: float | None,
    ) -> Any:
        resolved_input = resolve_references(step.input, run.inputs, outputs)
        step_run.input = resolved_input
        step_run.status = RunStatus.running
        step_run.started_at = datetime.now(UTC)
        timeout = step.timeout_seconds or self.settings.default_step_timeout_seconds
        max_attempts = min(step.retry.max_attempts, self.settings.max_step_retries + 1)
        started = time.perf_counter()
        last_error: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            step_run.attempts = attempt
            await session.commit()
            try:
                remaining = remaining_time_budget(deadline)
                attempt_timeout = min(timeout, remaining) if remaining is not None else timeout
                result = await asyncio.wait_for(
                    self._dispatch(step, resolved_input, run.id), timeout=attempt_timeout
                )
                if lease_owner is not None:
                    await assert_active_lease(session, run.id, lease_owner)
                output = result.output if isinstance(result, LLMResult) else result
                step_run.output = output
                if isinstance(result, LLMResult):
                    step_run.prompt_tokens = result.prompt_tokens
                    step_run.completion_tokens = result.completion_tokens
                    step_run.estimated_cost_usd = result.estimated_cost_usd
                    step_run.provider = result.provider
                step_run.status = RunStatus.completed
                step_run.completed_at = datetime.now(UTC)
                step_run.latency_ms = round((time.perf_counter() - started) * 1000, 3)
                await session.commit()
                return output
            except WorkerLeaseLostError:
                await session.rollback()
                raise
            except Exception as exc:
                if (
                    isinstance(exc, TimeoutError)
                    and deadline is not None
                    and time.perf_counter() >= deadline
                ):
                    exc = WorkflowTimeBudgetExceededError(
                        "Workflow execution time budget exhausted"
                    )
                last_error = exc
                step_run.error = f"{type(exc).__name__}: {exc}"
                await session.commit()
                if isinstance(exc, WorkflowTimeBudgetExceededError):
                    break
                if attempt < max_attempts:
                    try:
                        await wait_for_retry(
                            step.retry.backoff_seconds * (2 ** (attempt - 1)), deadline
                        )
                    except WorkflowTimeBudgetExceededError as budget_error:
                        last_error = budget_error
                        break

        step_run.status = RunStatus.failed
        assert last_error is not None
        step_run.error = f"{type(last_error).__name__}: {last_error}"
        step_run.completed_at = datetime.now(UTC)
        step_run.latency_ms = round((time.perf_counter() - started) * 1000, 3)
        await session.commit()
        raise last_error

    async def _dispatch(self, step: WorkflowStep, value: Any, run_id: str) -> Any:
        if step.type == "llm":
            prompt = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
            provider_names = [step.provider or "fake", *step.fallback_providers]
            for provider_name in provider_names:
                try:
                    provider = self.providers.get(provider_name)
                    result = await provider.complete(
                        prompt,
                        metadata={"run_id": run_id, "step": step.key},
                    )
                    return replace(result, provider=provider_name)
                except Exception as exc:
                    logger.warning(
                        "llm_provider_failed",
                        extra={
                            "run_id": run_id,
                            "step_key": step.key,
                            "provider": provider_name,
                            "error_type": type(exc).__name__,
                        },
                    )
            provider_list = ", ".join(provider_names)
            raise AllProvidersFailedError(f"All LLM providers failed: {provider_list}")
        assert step.tool is not None
        return await self.tools.get(step.tool)(value)
