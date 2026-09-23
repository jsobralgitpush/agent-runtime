import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.config import Settings, get_settings
from app.database import SessionLocal
from app.engine import WorkflowEngine
from app.models import RunStatus, WorkflowRun
from app.schemas import WorkflowDefinition

logger = logging.getLogger(__name__)


async def claim_next_run(session: AsyncSession) -> WorkflowRun | None:
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

    run.status = RunStatus.running
    await session.commit()
    logger.info("workflow_run_claimed", extra={"run_id": run.id})
    return run


async def run_worker_once(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> bool:
    """Execute one queued run and return whether work was found."""
    async with session_factory() as session:
        run = await claim_next_run(session)
        if run is None:
            return False

        definition = WorkflowDefinition.model_validate(run.workflow.definition)
        await WorkflowEngine(settings).execute(session, run, definition)
        return True


async def run_worker() -> None:
    settings = get_settings()
    while True:
        processed = await run_worker_once(SessionLocal, settings)
        if not processed:
            await asyncio.sleep(settings.worker_poll_interval_seconds)


def main() -> None:
    asyncio.run(run_worker())
