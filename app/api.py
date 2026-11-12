from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.database import get_session
from app.engine import ApprovalStateError, WorkflowEngine
from app.metrics import METRICS_CONTENT_TYPE, refresh_queue_metrics, render_metrics
from app.models import (
    RunStatus,
    Workflow,
    WorkflowLifecycleAction,
    WorkflowLifecycleEvent,
    WorkflowRun,
)
from app.providers import build_provider_registry
from app.schemas import (
    ApprovalDecision,
    HealthRead,
    ProviderRead,
    ReadinessRead,
    RunCreate,
    RunRead,
    RunStatusCounts,
    ToolRead,
    WorkflowCreate,
    WorkflowDefinition,
    WorkflowLifecycleEventRead,
    WorkflowRead,
    WorkflowRunPage,
    WorkflowRunSummary,
    WorkflowValidationRead,
)
from app.tools import ToolRegistry
from app.tracing import capture_trace_context

router = APIRouter()
Session = Annotated[AsyncSession, Depends(get_session)]


def _prefers_async(value: str | None) -> bool:
    return bool(value and "respond-async" in {item.strip().lower() for item in value.split(",")})


def _normalize_history_bound(
    value: datetime | None,
    field: str,
    error_code: str = "invalid_run_time_range",
) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": error_code,
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
    error_code: str = "invalid_run_time_range",
) -> None:
    if lower is not None and upper is not None and lower >= upper:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": error_code,
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


async def _get_workflow_by_idempotency_key(
    session: AsyncSession,
    idempotency_key: str,
) -> Workflow | None:
    workflow: Workflow | None = await session.scalar(
        select(Workflow).where(Workflow.idempotency_key == idempotency_key)
    )
    return workflow


async def _get_workflow_for_update(
    session: AsyncSession,
    workflow_id: str,
) -> Workflow | None:
    workflow: Workflow | None = await session.scalar(
        select(Workflow).where(Workflow.id == workflow_id).with_for_update()
    )
    return workflow


def _resolve_workflow_creation_replay(
    workflow: Workflow,
    payload: WorkflowCreate,
) -> Workflow:
    if (
        workflow.name != payload.name
        or workflow.description != payload.description
        or workflow.definition != payload.definition.model_dump(mode="json")
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "idempotency_key_reused"},
        )
    return workflow


@router.get("/health", response_model=HealthRead, tags=["operations"])
async def health() -> HealthRead:
    return HealthRead()


@router.get("/ready", response_model=ReadinessRead, tags=["operations"])
async def readiness(session: Session) -> ReadinessRead:
    try:
        await session.execute(select(1))
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "database_unavailable"},
        ) from exc
    return ReadinessRead()


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
async def create_workflow(
    payload: WorkflowCreate,
    session: Session,
    idempotency_key: Annotated[str | None, Header(min_length=1, max_length=255)] = None,
) -> Workflow:
    if idempotency_key is not None:
        existing = await _get_workflow_by_idempotency_key(session, idempotency_key)
        if existing is not None:
            return _resolve_workflow_creation_replay(existing, payload)

    _validate_workflow_extensions(payload.definition)
    workflow = Workflow(
        name=payload.name,
        idempotency_key=idempotency_key,
        description=payload.description,
        definition=payload.definition.model_dump(mode="json"),
    )
    session.add(workflow)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        if idempotency_key is not None:
            existing = await _get_workflow_by_idempotency_key(session, idempotency_key)
            if existing is not None:
                return _resolve_workflow_creation_replay(existing, payload)
        raise
    await session.refresh(workflow)
    return workflow


@router.get("/workflows", response_model=list[WorkflowRead])
async def list_workflows(
    session: Session,
    response: Response,
    name: Annotated[str | None, Query(min_length=1, max_length=120)] = None,
    archived: bool | None = None,
    limit: Annotated[int | None, Query(ge=1, le=100)] = None,
    cursor: Annotated[str | None, Query(max_length=36)] = None,
) -> list[Workflow]:
    if cursor is not None and limit is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "workflow_pagination_limit_required"},
        )

    query = select(Workflow)
    is_sqlite = session.get_bind().dialect.name == "sqlite"
    if name is not None:
        query = query.where(Workflow.name == name)
    if archived is True:
        query = query.where(Workflow.archived_at.is_not(None))
    elif archived is False:
        query = query.where(Workflow.archived_at.is_(None))
    if cursor is not None:
        cursor_workflow = await session.get(Workflow, cursor)
        if cursor_workflow is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"code": "invalid_workflow_cursor"},
            )
        if is_sqlite:
            created_key = func.julianday(Workflow.created_at)
            cursor_key = func.julianday(cursor_workflow.created_at)
            query = query.where(
                or_(
                    created_key < cursor_key,
                    and_(created_key == cursor_key, Workflow.id < cursor_workflow.id),
                )
            )
        else:
            query = query.where(
                or_(
                    Workflow.created_at < cursor_workflow.created_at,
                    and_(
                        Workflow.created_at == cursor_workflow.created_at,
                        Workflow.id < cursor_workflow.id,
                    ),
                )
            )

    if is_sqlite:
        query = query.order_by(func.julianday(Workflow.created_at).desc(), Workflow.id.desc())
    else:
        query = query.order_by(Workflow.created_at.desc(), Workflow.id.desc())
    if limit is None:
        return list(await session.scalars(query))

    workflows = list(await session.scalars(query.limit(limit + 1)))
    items = workflows[:limit]
    if len(workflows) > limit:
        response.headers["X-Next-Cursor"] = items[-1].id
    return items


@router.get("/workflows/{workflow_id}", response_model=WorkflowRead)
async def get_workflow(workflow_id: str, session: Session) -> Workflow:
    workflow = await session.get(Workflow, workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return workflow


@router.post("/workflows/{workflow_id}/archive", response_model=WorkflowRead)
async def archive_workflow(workflow_id: str, session: Session) -> Workflow:
    workflow = await _get_workflow_for_update(session, workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    if workflow.archived_at is None:
        archived_at = datetime.now(UTC)
        workflow.archived_at = archived_at
        session.add(
            WorkflowLifecycleEvent(
                workflow_id=workflow.id,
                action=WorkflowLifecycleAction.archived,
                created_at=archived_at,
            )
        )
        await session.commit()
        await session.refresh(workflow)
    return workflow


@router.post("/workflows/{workflow_id}/restore", response_model=WorkflowRead)
async def restore_workflow(workflow_id: str, session: Session) -> Workflow:
    workflow = await _get_workflow_for_update(session, workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    if workflow.archived_at is not None:
        _validate_workflow_extensions(WorkflowDefinition.model_validate(workflow.definition))
        workflow.archived_at = None
        session.add(
            WorkflowLifecycleEvent(
                workflow_id=workflow.id,
                action=WorkflowLifecycleAction.restored,
                created_at=datetime.now(UTC),
            )
        )
        await session.commit()
        await session.refresh(workflow)
    return workflow


@router.get(
    "/workflows/{workflow_id}/lifecycle-events",
    response_model=list[WorkflowLifecycleEventRead],
)
async def list_workflow_lifecycle_events(
    workflow_id: str,
    session: Session,
    response: Response,
    action: WorkflowLifecycleAction | None = None,
    created_from: datetime | None = None,
    created_before: datetime | None = None,
    limit: Annotated[int | None, Query(ge=1, le=100)] = None,
    cursor: Annotated[str | None, Query(max_length=36)] = None,
) -> list[WorkflowLifecycleEvent]:
    time_range_error = "invalid_workflow_lifecycle_event_time_range"
    created_from = _normalize_history_bound(created_from, "created_from", time_range_error)
    created_before = _normalize_history_bound(created_before, "created_before", time_range_error)
    _validate_history_range(
        created_from,
        created_before,
        "created_from",
        "created_before",
        time_range_error,
    )
    if cursor is not None and limit is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "workflow_lifecycle_event_pagination_limit_required"},
        )
    if await session.get(Workflow, workflow_id) is None:
        raise HTTPException(status_code=404, detail="Workflow not found")

    query = select(WorkflowLifecycleEvent).where(WorkflowLifecycleEvent.workflow_id == workflow_id)
    if action is not None:
        query = query.where(WorkflowLifecycleEvent.action == action)
    is_sqlite = session.get_bind().dialect.name == "sqlite"
    if is_sqlite:
        created_key = func.julianday(WorkflowLifecycleEvent.created_at)
        if created_from is not None:
            query = query.where(created_key >= func.julianday(created_from))
        if created_before is not None:
            query = query.where(created_key < func.julianday(created_before))
    else:
        if created_from is not None:
            query = query.where(WorkflowLifecycleEvent.created_at >= created_from)
        if created_before is not None:
            query = query.where(WorkflowLifecycleEvent.created_at < created_before)
    if cursor is not None:
        cursor_event = await session.get(WorkflowLifecycleEvent, cursor)
        if cursor_event is None or cursor_event.workflow_id != workflow_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"code": "invalid_workflow_lifecycle_event_cursor"},
            )
        if is_sqlite:
            cursor_key = func.julianday(cursor_event.created_at)
            query = query.where(
                or_(
                    created_key > cursor_key,
                    and_(
                        created_key == cursor_key,
                        WorkflowLifecycleEvent.id > cursor_event.id,
                    ),
                )
            )
        else:
            query = query.where(
                or_(
                    WorkflowLifecycleEvent.created_at > cursor_event.created_at,
                    and_(
                        WorkflowLifecycleEvent.created_at == cursor_event.created_at,
                        WorkflowLifecycleEvent.id > cursor_event.id,
                    ),
                )
            )

    if is_sqlite:
        query = query.order_by(
            func.julianday(WorkflowLifecycleEvent.created_at),
            WorkflowLifecycleEvent.id,
        )
    else:
        query = query.order_by(
            WorkflowLifecycleEvent.created_at,
            WorkflowLifecycleEvent.id,
        )
    if limit is None:
        return list(await session.scalars(query))

    events = list(await session.scalars(query.limit(limit + 1)))
    items = events[:limit]
    if len(events) > limit:
        response.headers["X-Next-Cursor"] = items[-1].id
    return items


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


@router.get(
    "/workflows/{workflow_id}/runs/summary",
    response_model=WorkflowRunSummary,
)
async def summarize_workflow_runs(
    workflow_id: str,
    session: Session,
    created_from: datetime | None = None,
    created_before: datetime | None = None,
    completed_from: datetime | None = None,
    completed_before: datetime | None = None,
) -> WorkflowRunSummary:
    created_from = _normalize_history_bound(created_from, "created_from")
    created_before = _normalize_history_bound(created_before, "created_before")
    completed_from = _normalize_history_bound(completed_from, "completed_from")
    completed_before = _normalize_history_bound(completed_before, "completed_before")
    _validate_history_range(created_from, created_before, "created_from", "created_before")
    _validate_history_range(completed_from, completed_before, "completed_from", "completed_before")

    if await session.get(Workflow, workflow_id) is None:
        raise HTTPException(status_code=404, detail="Workflow not found")

    conditions = [WorkflowRun.workflow_id == workflow_id]
    if created_from is not None:
        conditions.append(WorkflowRun.created_at >= created_from)
    if created_before is not None:
        conditions.append(WorkflowRun.created_at < created_before)
    if completed_from is not None:
        conditions.append(WorkflowRun.completed_at >= completed_from)
    if completed_before is not None:
        conditions.append(WorkflowRun.completed_at < completed_before)

    result = await session.execute(
        select(WorkflowRun.status, func.count(WorkflowRun.id))
        .where(*conditions)
        .group_by(WorkflowRun.status)
    )
    counts_by_status = {run_status: count for run_status, count in result.all()}
    counts = RunStatusCounts(
        pending=counts_by_status.get(RunStatus.pending, 0),
        running=counts_by_status.get(RunStatus.running, 0),
        waiting=counts_by_status.get(RunStatus.waiting, 0),
        completed=counts_by_status.get(RunStatus.completed, 0),
        failed=counts_by_status.get(RunStatus.failed, 0),
    )
    return WorkflowRunSummary(
        workflow_id=workflow_id,
        total=sum(counts_by_status.values()),
        counts=counts,
    )


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
    workflow = await _get_workflow_for_update(session, workflow_id)
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

    if workflow.archived_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "workflow_archived"},
        )

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
