import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode, Tracer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.metrics import record_step_attempt, record_workflow_execution
from app.models import RunStatus, StepRun, WorkflowRun
from app.providers import LLMResult, ProviderRegistry, build_provider_registry
from app.schemas import WorkflowDefinition, WorkflowStep
from app.tools import ToolRegistry

logger = logging.getLogger(__name__)
REFERENCE = re.compile(r"\$\{(input|steps|runtime)\.([a-zA-Z0-9_-]+)(?:\.output)?\}")


class WorkerLeaseLostError(RuntimeError):
    pass


class AllProvidersFailedError(RuntimeError):
    pass


class WorkflowTimeBudgetExceededError(TimeoutError):
    pass


class WorkflowTokenBudgetExceededError(RuntimeError):
    pass


class WorkflowCostBudgetExceededError(RuntimeError):
    pass


class WorkflowCostUnavailableError(RuntimeError):
    pass


class ApprovalStateError(RuntimeError):
    pass


def token_budget_exceeded(
    consumed_tokens: int, max_total_tokens: int
) -> WorkflowTokenBudgetExceededError:
    return WorkflowTokenBudgetExceededError(
        f"Workflow token budget of {max_total_tokens} exhausted after "
        f"consuming {consumed_tokens} tokens"
    )


def _format_usd(value: Decimal) -> str:
    return format(value.normalize(), "f")


def cost_budget_exceeded(
    consumed_cost_usd: Decimal, max_cost_usd: Decimal
) -> WorkflowCostBudgetExceededError:
    return WorkflowCostBudgetExceededError(
        f"Workflow cost budget of ${_format_usd(max_cost_usd)} exhausted after "
        f"an estimated ${_format_usd(consumed_cost_usd)}"
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


def _lookup_reference(
    match: re.Match[str],
    inputs: dict[str, Any],
    outputs: dict[str, Any],
    runtime: dict[str, str] | None,
) -> Any:
    namespace, key = match.group(1), match.group(2)
    sources: dict[str, dict[str, Any]] = {
        "input": inputs,
        "steps": outputs,
        "runtime": runtime or {},
    }
    source = sources[namespace]
    if key not in source:
        raise ValueError(f"Unknown workflow reference: {match.group(0)}")
    return source[key]


def resolve_references(
    value: Any,
    inputs: dict[str, Any],
    outputs: dict[str, Any],
    runtime: dict[str, str] | None = None,
) -> Any:
    if isinstance(value, dict):
        return {
            key: resolve_references(item, inputs, outputs, runtime) for key, item in value.items()
        }
    if isinstance(value, list):
        return [resolve_references(item, inputs, outputs, runtime) for item in value]
    if not isinstance(value, str):
        return value
    full_match = REFERENCE.fullmatch(value)
    if full_match:
        return _lookup_reference(full_match, inputs, outputs, runtime)

    def replace(match: re.Match[str]) -> str:
        resolved = _lookup_reference(match, inputs, outputs, runtime)
        return resolved if isinstance(resolved, str) else json.dumps(resolved, sort_keys=True)

    return REFERENCE.sub(replace, value)


def _runtime_context(run: WorkflowRun, step: WorkflowStep) -> dict[str, str]:
    return {
        "run_id": run.id,
        "step_key": step.key,
        "operation_key": f"{run.id}:{step.key}",
    }


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
        started = time.perf_counter()
        mode = "worker" if lease_owner is not None else "synchronous"
        result_status = "error"
        attributes = {
            "agent_runtime.run.id": run.id,
            "agent_runtime.workflow.id": run.workflow_id,
            "agent_runtime.run.mode": mode,
        }
        try:
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
                result_status = result.status.value
                span.set_attribute("agent_runtime.run.status", result_status)
                if result.status == RunStatus.failed:
                    span.set_status(Status(StatusCode.ERROR))
                return result
        finally:
            record_workflow_execution(
                mode,
                result_status,
                time.perf_counter() - started,
            )

    async def decide_approval(
        self,
        session: AsyncSession,
        run: WorkflowRun,
        definition: WorkflowDefinition,
        *,
        decision: Literal["approve", "reject"],
        comment: str | None,
        defer: bool = False,
        continuation_trace_context: dict[str, str] | None = None,
    ) -> WorkflowRun:
        locked_status = await session.scalar(
            select(WorkflowRun.status).where(WorkflowRun.id == run.id).with_for_update()
        )
        if locked_status != RunStatus.waiting:
            raise ApprovalStateError("Run is not waiting for approval")
        waiting_steps = list(
            await session.scalars(
                select(StepRun).where(
                    StepRun.run_id == run.id,
                    StepRun.step_type == "approval",
                    StepRun.status == RunStatus.waiting,
                )
            )
        )
        if len(waiting_steps) != 1:
            raise ApprovalStateError("Run has no single pending approval")

        approval_step = waiting_steps[0]
        approval_step.output = {"decision": decision, "comment": comment}
        approval_step.completed_at = datetime.now(UTC)
        if decision == "reject":
            error = "HumanApprovalRejected: approval was rejected"
            approval_step.status = RunStatus.failed
            approval_step.error = error
            run.status = RunStatus.failed
            run.error = error
            run.completed_at = approval_step.completed_at
            await session.commit()
            await session.refresh(run, attribute_names=["steps"])
            return run

        approval_step.status = RunStatus.completed
        if defer:
            run.status = RunStatus.pending
            run.trace_context = continuation_trace_context
            await session.commit()
            await session.refresh(run, attribute_names=["steps"])
            return run
        run.status = RunStatus.running
        await session.commit()
        return await self.execute(session, run, definition)

    async def _execute_workflow(
        self,
        session: AsyncSession,
        run: WorkflowRun,
        definition: WorkflowDefinition,
        *,
        lease_owner: str | None = None,
    ) -> WorkflowRun:
        existing_step_runs = list(
            await session.scalars(
                select(StepRun).where(StepRun.run_id == run.id).order_by(StepRun.position)
            )
        )
        completed_step_runs = {
            step_run.step_key: step_run
            for step_run in existing_step_runs
            if step_run.status == RunStatus.completed
        }
        incomplete_step_runs = {
            step_run.step_key: step_run
            for step_run in existing_step_runs
            if step_run.status != RunStatus.completed
        }
        outputs = {step_key: step_run.output for step_key, step_run in completed_step_runs.items()}
        consumed_tokens = sum(
            (step_run.prompt_tokens or 0) + (step_run.completion_tokens or 0)
            for step_run in completed_step_runs.values()
        )
        consumed_cost_usd = sum(
            (
                (
                    Decimal(str(step_run.estimated_cost_usd))
                    if step_run.estimated_cost_usd is not None
                    else Decimal(0)
                )
                for step_run in completed_step_runs.values()
            ),
            Decimal(0),
        )
        max_cost_usd = (
            Decimal(str(definition.max_estimated_cost_usd))
            if definition.max_estimated_cost_usd is not None
            else None
        )
        lease_lost = False
        run.status = RunStatus.running
        run.error = None
        run.completed_at = None
        if run.started_at is None:
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
            position_by_key = {
                step.key: position for position, step in enumerate(definition.execution_steps())
            }
            step_by_key = {step.key: step for step in definition.steps}
            if max_cost_usd is not None:
                missing_cost = next(
                    (
                        step_key
                        for step_key, step_run in completed_step_runs.items()
                        if step_by_key[step_key].type == "llm"
                        and step_run.estimated_cost_usd is None
                    ),
                    None,
                )
                if missing_cost is not None:
                    raise WorkflowCostUnavailableError(
                        "Cannot enforce workflow cost budget because completed step "
                        f"{missing_cost!r} has no estimated cost"
                    )
            waiting_for_approval = False
            for batch in definition.execution_batches():
                batch = [step for step in batch if step.key not in completed_step_runs]
                if not batch:
                    continue
                remaining_time_budget(deadline)
                for step in batch:
                    if (
                        step.type == "llm"
                        and definition.max_total_tokens is not None
                        and consumed_tokens >= definition.max_total_tokens
                    ):
                        raise token_budget_exceeded(
                            consumed_tokens,
                            definition.max_total_tokens,
                        )
                    if (
                        step.type == "llm"
                        and max_cost_usd is not None
                        and consumed_cost_usd >= max_cost_usd
                    ):
                        raise cost_budget_exceeded(consumed_cost_usd, max_cost_usd)

                if len(batch) == 1:
                    step = batch[0]
                    step_run = incomplete_step_runs.get(step.key)
                    if step_run is None:
                        step_run = StepRun(
                            run_id=run.id,
                            step_key=step.key,
                            position=position_by_key[step.key],
                            step_type=step.type,
                            status=RunStatus.pending,
                        )
                        session.add(step_run)
                    await session.flush()
                    if step.type == "approval":
                        step_run.input = resolve_references(
                            step.input,
                            run.inputs,
                            outputs,
                            _runtime_context(run, step),
                        )
                        step_run.status = RunStatus.waiting
                        step_run.started_at = datetime.now(UTC)
                        if lease_owner is not None:
                            await assert_active_lease(session, run.id, lease_owner)
                        run.status = RunStatus.waiting
                        run.lease_owner = None
                        run.lease_expires_at = None
                        await session.commit()
                        waiting_for_approval = True
                        break
                    output = await self._execute_step(
                        session,
                        run,
                        step,
                        step_run,
                        outputs,
                        lease_owner,
                        deadline,
                    )
                    completed_steps = [(step, step_run, output)]
                else:
                    completed_steps = await self._execute_parallel_batch(
                        session,
                        run,
                        batch,
                        position_by_key,
                        outputs,
                        incomplete_step_runs,
                        lease_owner,
                        deadline,
                    )

                for step, step_run, output in completed_steps:
                    consumed_tokens += (step_run.prompt_tokens or 0) + (
                        step_run.completion_tokens or 0
                    )
                    if step.type == "llm" and max_cost_usd is not None:
                        if step_run.estimated_cost_usd is None:
                            raise WorkflowCostUnavailableError(
                                "Cannot enforce workflow cost budget because provider "
                                f"{step_run.provider!r} did not report an estimated cost for step "
                                f"{step.key!r}"
                            )
                        consumed_cost_usd += Decimal(str(step_run.estimated_cost_usd))
                    outputs[step.key] = output
                if (
                    definition.max_total_tokens is not None
                    and consumed_tokens > definition.max_total_tokens
                ):
                    raise token_budget_exceeded(
                        consumed_tokens,
                        definition.max_total_tokens,
                    )
                if max_cost_usd is not None and consumed_cost_usd > max_cost_usd:
                    raise cost_budget_exceeded(consumed_cost_usd, max_cost_usd)
            if not waiting_for_approval:
                run.status = RunStatus.completed
                run.output = outputs[definition.output_step().key]
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
                if run.status in (RunStatus.completed, RunStatus.failed):
                    run.completed_at = datetime.now(UTC)
                await session.commit()
            await session.refresh(run, attribute_names=["steps"])

        logger.info("workflow_run_finished", extra={"run_id": run.id, "status": run.status.value})
        return run

    async def _execute_parallel_batch(
        self,
        session: AsyncSession,
        run: WorkflowRun,
        steps: list[WorkflowStep],
        position_by_key: dict[str, int],
        outputs: dict[str, Any],
        incomplete_step_runs: dict[str, StepRun],
        lease_owner: str | None,
        deadline: float | None,
    ) -> list[tuple[WorkflowStep, StepRun, Any]]:
        step_runs: list[StepRun] = []
        resolved_inputs: list[Any] = []
        preparation_errors: dict[int, Exception] = {}
        for offset, step in enumerate(steps):
            step_run = incomplete_step_runs.get(step.key)
            if step_run is None:
                step_run = StepRun(
                    run_id=run.id,
                    step_key=step.key,
                    position=position_by_key[step.key],
                    step_type=step.type,
                )
                session.add(step_run)
            step_run.status = RunStatus.running
            step_run.error = None
            step_run.started_at = datetime.now(UTC)
            step_runs.append(step_run)
            try:
                resolved_input = resolve_references(
                    step.input,
                    run.inputs,
                    outputs,
                    _runtime_context(run, step),
                )
            except Exception as exc:
                step_run.status = RunStatus.failed
                step_run.error = f"{type(exc).__name__}: {exc}"
                step_run.completed_at = datetime.now(UTC)
                step_run.latency_ms = 0
                preparation_errors[offset] = exc
                resolved_inputs.append(None)
            else:
                step_run.input = resolved_input
                resolved_inputs.append(resolved_input)
        await session.commit()

        task_indices = [index for index in range(len(steps)) if index not in preparation_errors]
        task_results = await asyncio.gather(
            *(
                self._execute_parallel_step(
                    run,
                    steps[index],
                    step_runs[index],
                    resolved_inputs[index],
                    deadline,
                )
                for index in task_indices
            ),
            return_exceptions=True,
        )
        results: list[Any | BaseException] = [
            preparation_errors.get(index) for index in range(len(steps))
        ]
        for index, result in zip(task_indices, task_results, strict=True):
            results[index] = result

        if lease_owner is not None:
            await assert_active_lease(session, run.id, lease_owner)
        await session.commit()

        for result in results:
            if isinstance(result, BaseException):
                raise result
        return [
            (step, step_run, result)
            for step, step_run, result in zip(steps, step_runs, results, strict=True)
        ]

    async def _execute_parallel_step(
        self,
        run: WorkflowRun,
        step: WorkflowStep,
        step_run: StepRun,
        resolved_input: Any,
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
                output = await self._execute_parallel_step_inner(
                    run,
                    step,
                    step_run,
                    resolved_input,
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

    async def _execute_parallel_step_inner(
        self,
        run: WorkflowRun,
        step: WorkflowStep,
        step_run: StepRun,
        resolved_input: Any,
        deadline: float | None,
    ) -> Any:
        return await self._run_step_attempts(
            run,
            step,
            step_run,
            resolved_input,
            deadline,
        )

    async def _run_step_attempts(
        self,
        run: WorkflowRun,
        step: WorkflowStep,
        step_run: StepRun,
        resolved_input: Any,
        deadline: float | None,
        *,
        persist_state: Callable[[], Awaitable[None]] | None = None,
        verify_result: Callable[[], Awaitable[None]] | None = None,
    ) -> Any:
        timeout = step.timeout_seconds or self.settings.default_step_timeout_seconds
        max_attempts = min(step.retry.max_attempts, self.settings.max_step_retries + 1)
        started = time.perf_counter()
        last_error: Exception | None = None
        prior_attempts = step_run.attempts

        for attempt in range(1, max_attempts + 1):
            step_run.attempts = prior_attempts + attempt
            if persist_state is not None:
                await persist_state()
            try:
                remaining = remaining_time_budget(deadline)
                attempt_timeout = min(timeout, remaining) if remaining is not None else timeout
                result = await asyncio.wait_for(
                    self._dispatch(step, resolved_input, run.id), timeout=attempt_timeout
                )
                if verify_result is not None:
                    await verify_result()
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
                if persist_state is not None:
                    await persist_state()
                record_step_attempt(step.type, "success")
                return output
            except WorkerLeaseLostError:
                record_step_attempt(step.type, "lease_lost")
                raise
            except Exception as exc:
                record_step_attempt(step.type, "error")
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
                if persist_state is not None:
                    await persist_state()
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
        if persist_state is not None:
            await persist_state()
        raise last_error

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
        resolved_input = resolve_references(
            step.input,
            run.inputs,
            outputs,
            _runtime_context(run, step),
        )
        step_run.input = resolved_input
        step_run.status = RunStatus.running
        step_run.error = None
        step_run.started_at = datetime.now(UTC)

        async def verify_result() -> None:
            if lease_owner is not None:
                await assert_active_lease(session, run.id, lease_owner)

        return await self._run_step_attempts(
            run,
            step,
            step_run,
            resolved_input,
            deadline,
            persist_state=session.commit,
            verify_result=verify_result,
        )

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
