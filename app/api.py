from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.database import get_session
from app.engine import ApprovalStateError, WorkflowEngine
from app.metrics import METRICS_CONTENT_TYPE, refresh_queue_metrics, render_metrics
from app.models import RunStatus, Workflow, WorkflowRun
from app.providers import build_provider_registry
from app.schemas import (
    ApprovalDecision,
    HealthRead,
    ProviderRead,
    RunCreate,
    RunRead,
    WorkflowCreate,
    WorkflowDefinition,
    WorkflowRead,
)
from app.tracing import capture_trace_context

router = APIRouter()
Session = Annotated[AsyncSession, Depends(get_session)]


def _prefers_async(value: str | None) -> bool:
    return bool(value and "respond-async" in {item.strip().lower() for item in value.split(",")})


@router.get("/health", response_model=HealthRead, tags=["operations"])
async def health() -> HealthRead:
    return HealthRead()


@router.get("/metrics", tags=["operations"], response_class=Response)
async def metrics(session: Session) -> Response:
    await refresh_queue_metrics(session)
    return Response(
        content=render_metrics(),
        headers={"Content-Type": METRICS_CONTENT_TYPE},
    )


@router.get("/providers", response_model=list[ProviderRead], tags=["operations"])
async def list_providers() -> list[ProviderRead]:
    registry = build_provider_registry(get_settings())
    return [ProviderRead(name=name) for name in registry.names()]


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
    response: Response,
    idempotency_key: Annotated[str | None, Header(max_length=255)] = None,
    prefer: Annotated[str | None, Header(max_length=255)] = None,
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

    defer = _prefers_async(prefer)
    run = WorkflowRun(
        workflow_id=workflow_id,
        inputs=payload.inputs,
        idempotency_key=idempotency_key,
        trace_context=capture_trace_context() if defer else None,
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
    if defer:
        response.status_code = status.HTTP_202_ACCEPTED
        await session.refresh(run, attribute_names=["steps"])
        return run

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


@router.post("/runs/{run_id}/approval", response_model=RunRead)
async def decide_approval(
    run_id: str,
    payload: ApprovalDecision,
    session: Session,
    response: Response,
    prefer: Annotated[str | None, Header(max_length=255)] = None,
) -> WorkflowRun:
    defer = _prefers_async(prefer)
    run = await session.scalar(
        select(WorkflowRun)
        .where(WorkflowRun.id == run_id)
        .options(selectinload(WorkflowRun.workflow))
        .with_for_update()
    )
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    definition = WorkflowDefinition.model_validate(run.workflow.definition)
    try:
        result = await WorkflowEngine(get_settings()).decide_approval(
            session,
            run,
            definition,
            decision=payload.decision,
            comment=payload.comment,
            defer=defer,
            continuation_trace_context=capture_trace_context() if defer else None,
        )
        if result.status == RunStatus.pending:
            response.status_code = status.HTTP_202_ACCEPTED
        return result
    except ApprovalStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
