from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.database import get_session
from app.engine import WorkflowEngine
from app.models import Workflow, WorkflowRun
from app.schemas import (
    HealthRead,
    RunCreate,
    RunRead,
    WorkflowCreate,
    WorkflowDefinition,
    WorkflowRead,
)

router = APIRouter()
Session = Annotated[AsyncSession, Depends(get_session)]


@router.get("/health", response_model=HealthRead, tags=["operations"])
async def health() -> HealthRead:
    return HealthRead()


@router.post("/workflows", response_model=WorkflowRead, status_code=status.HTTP_201_CREATED)
async def create_workflow(payload: WorkflowCreate, session: Session) -> Workflow:
    workflow = Workflow(
        name=payload.name,
        description=payload.description,
        definition=payload.definition.model_dump(mode="json"),
    )
    session.add(workflow)
    await session.commit()
    await session.refresh(workflow)
    return workflow


@router.get("/workflows", response_model=list[WorkflowRead])
async def list_workflows(session: Session) -> list[Workflow]:
    result = await session.scalars(select(Workflow).order_by(Workflow.created_at.desc()))
    return list(result)


@router.get("/workflows/{workflow_id}", response_model=WorkflowRead)
async def get_workflow(workflow_id: str, session: Session) -> Workflow:
    workflow = await session.get(Workflow, workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return workflow


async def _get_run(session: AsyncSession, run_id: str) -> WorkflowRun | None:
    query = (
        select(WorkflowRun).where(WorkflowRun.id == run_id).options(selectinload(WorkflowRun.steps))
    )
    result: WorkflowRun | None = await session.scalar(query)
    return result


@router.post(
    "/workflows/{workflow_id}/runs",
    response_model=RunRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_run(
    workflow_id: str,
    payload: RunCreate,
    session: Session,
    idempotency_key: Annotated[str | None, Header(max_length=255)] = None,
) -> WorkflowRun:
    workflow = await session.get(Workflow, workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found")

    if idempotency_key:
        existing: WorkflowRun | None = await session.scalar(
            select(WorkflowRun)
            .where(
                WorkflowRun.workflow_id == workflow_id,
                WorkflowRun.idempotency_key == idempotency_key,
            )
            .options(selectinload(WorkflowRun.steps))
        )
        if existing:
            return existing

    run = WorkflowRun(
        workflow_id=workflow_id,
        inputs=payload.inputs,
        idempotency_key=idempotency_key,
    )
    session.add(run)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = await session.scalar(
            select(WorkflowRun)
            .where(
                WorkflowRun.workflow_id == workflow_id,
                WorkflowRun.idempotency_key == idempotency_key,
            )
            .options(selectinload(WorkflowRun.steps))
        )
        if existing:
            return existing
        raise

    await session.refresh(run)
    engine = WorkflowEngine(get_settings())
    return await engine.execute(
        session, run, WorkflowDefinition.model_validate(workflow.definition)
    )


@router.get("/runs/{run_id}", response_model=RunRead)
async def get_run(run_id: str, session: Session) -> WorkflowRun:
    run = await _get_run(session, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return run
