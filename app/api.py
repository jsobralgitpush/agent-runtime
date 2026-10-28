from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from sqlalchemy import and_, or_, select
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
    ToolRead,
    WorkflowCreate,
    WorkflowDefinition,
    WorkflowRead,
    WorkflowRunPage,
    WorkflowValidationRead,
)
from app.tools import ToolRegistry
from app.tracing import capture_trace_context

router = APIRouter()
Session = Annotated[AsyncSession, Depends(get_session)]


def _prefers_async(value: str | None) -> bool:
    return bool(value and "respond-async" in {item.strip().lower() for item in value.split(",")})


def _normalize_history_bound(value: datetime | None, field: str) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "invalid_run_time_range",
                "reason": "timezone_required",
                "field": field,
            },
        )
    return value.astimezone(UTC)


def _validate_history_range(
    lower: datetime | None,
    upper: datetime | None,
    lower_field: str,
    upper_field: str,
) -> None:
    if lower is not None and upper is not None and lower >= upper:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "invalid_run_time_range",
                "reason": f"{lower_field}_must_precede_{upper_field}",
            },
        )


def _validate_workflow_extensions(definition: WorkflowDefinition) -> None:
    provider_names = set(build_provider_registry(get_settings()).names())
    tool_names = set(ToolRegistry().names())
    requested_providers = {
        provider_name
        for step in definition.steps
        if step.type == "llm"
        for provider_name in (step.provider or "fake", *step.fallback_providers)
    }
    requested_tools = {
        step.tool for step in definition.steps if step.type == "tool" and step.tool is not None
    }
    unknown_providers = sorted(requested_providers - provider_names)
    unknown_tools = sorted(requested_tools - tool_names)
    if unknown_providers or unknown_tools:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "unknown_workflow_extensions",
                "unknown_providers": unknown_providers,
                "unknown_tools": unknown_tools,
            },
        )


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


@router.get("/tools", response_model=list[ToolRead], tags=["operations"])
async def list_tools() -> list[ToolRead]:
    registry = ToolRegistry()
    return [ToolRead(name=name) for name in registry.names()]


@router.post("/workflows/validate", response_model=WorkflowValidationRead)
async def validate_workflow(payload: WorkflowCreate) -> WorkflowValidationRead:
    _validate_workflow_extensions(payload.definition)
    return WorkflowValidationRead()


@router.post("/workflows", response_model=WorkflowRead, status_code=status.HTTP_201_CREATED)
async def create_workflow(payload: WorkflowCreate, session: Session) -> Workflow:
    _validate_workflow_extensions(payload.definition)
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


@router.get("/workflows/{workflow_id}/runs", response_model=WorkflowRunPage)
async def list_workflow_runs(
    workflow_id: str,
    session: Session,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(max_length=36)] = None,
    idempotency_key: Annotated[str | None, Query(max_length=255)] = None,
    run_statuses: Annotated[list[RunStatus] | None, Query(alias="status")] = None,
    created_from: datetime | None = None,
    created_before: datetime | None = None,
    completed_from: datetime | None = None,
    completed_before: datetime | None = None,
) -> WorkflowRunPage:
    created_from = _normalize_history_bound(created_from, "created_from")
    created_before = _normalize_history_bound(created_before, "created_before")
    completed_from = _normalize_history_bound(completed_from, "completed_from")
    completed_before = _normalize_history_bound(completed_before, "completed_before")
    _validate_history_range(created_from, created_before, "created_from", "created_before")
    _validate_history_range(completed_from, completed_before, "completed_from", "completed_before")

    if await session.get(Workflow, workflow_id) is None:
        raise HTTPException(status_code=404, detail="Workflow not found")

    conditions = [WorkflowRun.workflow_id == workflow_id]
    if idempotency_key is not None:
        conditions.append(WorkflowRun.idempotency_key == idempotency_key)
    if run_statuses is not None:
        conditions.append(WorkflowRun.status.in_(run_statuses))
    if created_from is not None:
        conditions.append(WorkflowRun.created_at >= created_from)
    if created_before is not None:
        conditions.append(WorkflowRun.created_at < created_before)
    if completed_from is not None:
        conditions.append(WorkflowRun.completed_at >= completed_from)
    if completed_before is not None:
        conditions.append(WorkflowRun.completed_at < completed_before)
    if cursor is not None:
        cursor_run = await session.scalar(
            select(WorkflowRun).where(
                WorkflowRun.id == cursor,
                WorkflowRun.workflow_id == workflow_id,
            )
        )
        if cursor_run is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"code": "invalid_run_cursor"},
            )
        conditions.append(
            or_(
                WorkflowRun.created_at < cursor_run.created_at,
                and_(
                    WorkflowRun.created_at == cursor_run.created_at,
                    WorkflowRun.id < cursor_run.id,
                ),
            )
        )

    query = (
        select(WorkflowRun)
        .where(*conditions)
        .options(selectinload(WorkflowRun.steps))
        .order_by(WorkflowRun.created_at.desc(), WorkflowRun.id.desc())
        .limit(limit + 1)
    )
    runs = list(await session.scalars(query))
    items = runs[:limit]
    next_cursor = items[-1].id if len(runs) > limit else None
    return WorkflowRunPage(items=items, next_cursor=next_cursor)


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
