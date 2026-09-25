import asyncio
import json
import logging
import re
import time
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import RunStatus, StepRun, WorkflowRun
from app.providers import LLMResult, ProviderRegistry
from app.schemas import WorkflowDefinition, WorkflowStep
from app.tools import ToolRegistry

logger = logging.getLogger(__name__)
REFERENCE = re.compile(r"\$\{(input|steps)\.([a-zA-Z0-9_-]+)(?:\.output)?\}")


class WorkerLeaseLostError(RuntimeError):
    pass


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
    ) -> None:
        self.settings = settings
        self.providers = providers or ProviderRegistry()
        self.tools = tools or ToolRegistry()

    async def execute(
        self,
        session: AsyncSession,
        run: WorkflowRun,
        definition: WorkflowDefinition,
        *,
        lease_owner: str | None = None,
    ) -> WorkflowRun:
        outputs: dict[str, Any] = {}
        lease_lost = False
        run.status = RunStatus.running
        run.started_at = datetime.now(UTC)
        await session.commit()
        logger.info(
            "workflow_run_started", extra={"run_id": run.id, "workflow_id": run.workflow_id}
        )

        try:
            for position, step in enumerate(definition.steps):
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
                result = await asyncio.wait_for(
                    self._dispatch(step, resolved_input, run.id), timeout=timeout
                )
                if lease_owner is not None:
                    await assert_active_lease(session, run.id, lease_owner)
                output = result.output if isinstance(result, LLMResult) else result
                step_run.output = output
                if isinstance(result, LLMResult):
                    step_run.prompt_tokens = result.prompt_tokens
                    step_run.completion_tokens = result.completion_tokens
                    step_run.estimated_cost_usd = result.estimated_cost_usd
                step_run.status = RunStatus.completed
                step_run.completed_at = datetime.now(UTC)
                step_run.latency_ms = round((time.perf_counter() - started) * 1000, 3)
                await session.commit()
                return output
            except WorkerLeaseLostError:
                await session.rollback()
                raise
            except Exception as exc:
                last_error = exc
                step_run.error = f"{type(exc).__name__}: {exc}"
                await session.commit()
                if attempt < max_attempts:
                    await asyncio.sleep(step.retry.backoff_seconds * (2 ** (attempt - 1)))

        step_run.status = RunStatus.failed
        step_run.completed_at = datetime.now(UTC)
        step_run.latency_ms = round((time.perf_counter() - started) * 1000, 3)
        await session.commit()
        assert last_error is not None
        raise last_error

    async def _dispatch(self, step: WorkflowStep, value: Any, run_id: str) -> Any:
        if step.type == "llm":
            prompt = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
            provider = self.providers.get(step.provider or "fake")
            return await provider.complete(prompt, metadata={"run_id": run_id, "step": step.key})
        assert step.tool is not None
        return await self.tools.get(step.tool)(value)
