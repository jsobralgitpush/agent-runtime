import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.config import Settings, get_settings
from app.database import SessionLocal
from app.engine import WorkflowEngine
from app.metrics import (
    refresh_queue_metrics,
    start_worker_metrics_server,
    worker_active_runs,
    worker_claims_total,
    worker_recoveries_total,
)
from app.models import RunStatus, StepRun, WorkflowRun
from app.schemas import WorkflowDefinition
from app.tracing import configure_tracing, use_trace_context

logger = logging.getLogger(__name__)
LEASE_EXPIRED_ERROR = "WorkerLeaseExpired: worker stopped heartbeating; automatic replay disabled"


async def recover_expired_runs(
    session: AsyncSession,
    batch_size: int,
    *,
    now: datetime | None = None,
) -> int:
    """Requeue explicitly replay-safe work and fail every other abandoned run."""
    recovered_at = now or datetime.now(UTC)
    query = (
        select(WorkflowRun)
        .where(
            WorkflowRun.status == RunStatus.running,
            WorkflowRun.lease_expires_at.is_not(None),
            WorkflowRun.lease_expires_at < recovered_at,
        )
        .order_by(WorkflowRun.lease_expires_at, WorkflowRun.id)
        .options(selectinload(WorkflowRun.workflow), selectinload(WorkflowRun.steps))
        .with_for_update(skip_locked=True)
        .limit(batch_size)
    )
    runs = list(await session.scalars(query))
    if not runs:
        return 0

    failed_run_ids: list[str] = []
    requeued = 0
    for run in runs:
        definition = WorkflowDefinition.model_validate(run.workflow.definition)
        step_by_key = {step.key: step for step in definition.steps}
        incomplete_steps = [
            step for step in run.steps if step.status in (RunStatus.pending, RunStatus.running)
        ]
        can_replay = all(
            step.step_key in step_by_key and step_by_key[step.step_key].replay_safe
            for step in incomplete_steps
        )
        run.lease_owner = None
        run.lease_expires_at = None
        if can_replay:
            run.status = RunStatus.pending
            run.error = None
            run.completed_at = None
            for step in incomplete_steps:
                step.status = RunStatus.pending
                step.output = None
                step.error = None
                step.started_at = None
                step.completed_at = None
                step.latency_ms = None
                step.prompt_tokens = None
                step.completion_tokens = None
                step.estimated_cost_usd = None
                step.provider = None
            requeued += 1
        else:
            run.status = RunStatus.failed
            run.error = LEASE_EXPIRED_ERROR
            run.completed_at = recovered_at
            failed_run_ids.append(run.id)

    if failed_run_ids:
        await session.execute(
            update(StepRun)
            .where(
                StepRun.run_id.in_(failed_run_ids),
                StepRun.status.in_([RunStatus.pending, RunStatus.running]),
            )
            .values(
                status=RunStatus.failed,
                error=LEASE_EXPIRED_ERROR,
                completed_at=recovered_at,
            )
        )
    await session.commit()
    if failed_run_ids:
        logger.warning("expired_workflow_runs_failed", extra={"count": len(failed_run_ids)})
        worker_recoveries_total.labels(outcome="failed").inc(len(failed_run_ids))
    if requeued:
        logger.warning("expired_workflow_runs_requeued", extra={"count": requeued})
        worker_recoveries_total.labels(outcome="requeued").inc(requeued)
    return len(runs)


async def release_lease(session: AsyncSession, run_id: str, worker_id: str) -> None:
    await session.execute(
        update(WorkflowRun)
        .where(
            WorkflowRun.id == run_id,
            WorkflowRun.lease_owner == worker_id,
            WorkflowRun.status.in_([RunStatus.waiting, RunStatus.completed, RunStatus.failed]),
        )
        .values(lease_owner=None, lease_expires_at=None)
    )
    await session.commit()


async def claim_next_run(
    session: AsyncSession,
    worker_id: str,
    lease_seconds: int,
    *,
    now: datetime | None = None,
) -> WorkflowRun | None:
    """Claim the oldest pending run without blocking another worker."""
    query = (
        select(WorkflowRun)
        .where(WorkflowRun.status == RunStatus.pending)
        .order_by(WorkflowRun.created_at, WorkflowRun.id)
        .options(selectinload(WorkflowRun.workflow))
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    run = await session.scalar(query)
    if run is None:
        return None

    claimed_at = now or datetime.now(UTC)
    run.status = RunStatus.running
    run.lease_owner = worker_id
    run.heartbeat_at = claimed_at
    run.lease_expires_at = claimed_at + timedelta(seconds=lease_seconds)
    await session.commit()
    worker_claims_total.inc()
    logger.info("workflow_run_claimed", extra={"run_id": run.id, "worker_id": worker_id})
    return run


async def renew_lease(
    session: AsyncSession,
    run_id: str,
    worker_id: str,
    lease_seconds: int,
    *,
    now: datetime | None = None,
) -> bool:
    heartbeat_at = now or datetime.now(UTC)
    run = await session.scalar(
        select(WorkflowRun)
        .where(
            WorkflowRun.id == run_id,
            WorkflowRun.status == RunStatus.running,
            WorkflowRun.lease_owner == worker_id,
            WorkflowRun.lease_expires_at.is_not(None),
            WorkflowRun.lease_expires_at > heartbeat_at,
        )
        .with_for_update()
    )
    if run is None:
        await session.rollback()
        return False

    run.heartbeat_at = heartbeat_at
    run.lease_expires_at = heartbeat_at + timedelta(seconds=lease_seconds)
    await session.commit()
    return True


async def maintain_lease(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: str,
    settings: Settings,
) -> None:
    while True:
        await asyncio.sleep(settings.worker_heartbeat_seconds)
        async with session_factory() as session:
            renewed = await renew_lease(
                session,
                run_id,
                settings.worker_id,
                settings.worker_lease_seconds,
            )
            if settings.worker_metrics_port is not None:
                await refresh_queue_metrics(session)
        if not renewed:
            return


async def run_worker_once(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> bool:
    """Execute one queued run and return whether work was found."""
    async with session_factory() as session:
        await recover_expired_runs(session, settings.worker_recovery_batch_size)
        run = await claim_next_run(
            session,
            settings.worker_id,
            settings.worker_lease_seconds,
        )
        if settings.worker_metrics_port is not None:
            await refresh_queue_metrics(session)
        if run is None:
            return False

        definition = WorkflowDefinition.model_validate(run.workflow.definition)
        heartbeat = asyncio.create_task(maintain_lease(session_factory, run.id, settings))
        worker_active_runs.inc()
        try:
            with use_trace_context(run.trace_context):
                await WorkflowEngine(settings).execute(
                    session,
                    run,
                    definition,
                    lease_owner=settings.worker_id,
                )
        finally:
            worker_active_runs.dec()
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
            await release_lease(session, run.id, settings.worker_id)
        return True


async def run_worker() -> None:
    settings = get_settings()
    while True:
        processed = await run_worker_once(SessionLocal, settings)
        if not processed:
            await asyncio.sleep(settings.worker_poll_interval_seconds)


def main() -> None:
    settings = get_settings()
    tracer_provider = configure_tracing(settings)
    metrics_server = None
    try:
        if settings.worker_metrics_port is not None:
            metrics_server = start_worker_metrics_server(
                settings.worker_metrics_host,
                settings.worker_metrics_port,
            )
        asyncio.run(run_worker())
    finally:
        if tracer_provider is not None:
            tracer_provider.shutdown()
        if metrics_server is not None:
            server, thread = metrics_server
            server.shutdown()
            server.server_close()
            thread.join()
