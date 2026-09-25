from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

from app.config import Settings
from app.engine import WorkerLeaseLostError, assert_active_lease
from app.models import RunStatus, StepRun, WorkflowRun
from app.worker import (
    LEASE_EXPIRED_ERROR,
    claim_next_run,
    recover_expired_runs,
    renew_lease,
    run_worker_once,
)
from tests.conftest import TestSession


async def create_workflow(client: AsyncClient) -> dict[str, object]:
    response = await client.post(
        "/v1/workflows",
        json={
            "name": "Writer",
            "description": "A deterministic example",
            "definition": {
                "steps": [
                    {
                        "key": "draft",
                        "type": "llm",
                        "provider": "fake",
                        "input": "Explain ${input.topic}",
                    },
                    {
                        "key": "publish",
                        "type": "tool",
                        "tool": "uppercase",
                        "input": "${steps.draft.output}",
                    },
                ]
            },
        },
    )
    assert response.status_code == 201
    return response.json()


async def test_health(client: AsyncClient) -> None:
    response = await client.get("/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_create_list_and_get_workflow(client: AsyncClient) -> None:
    created = await create_workflow(client)
    listed = await client.get("/v1/workflows")
    fetched = await client.get(f"/v1/workflows/{created['id']}")
    assert listed.status_code == 200
    assert len(listed.json()) == 1
    assert fetched.json() == created


async def test_execute_workflow_and_record_metrics(client: AsyncClient) -> None:
    workflow = await create_workflow(client)
    response = await client.post(
        f"/v1/workflows/{workflow['id']}/runs",
        json={"inputs": {"topic": "idempotency"}},
        headers={"Idempotency-Key": "request-123"},
    )
    assert response.status_code == 201
    run = response.json()
    assert run["status"] == "completed"
    assert run["output"].endswith("EXPLAIN IDEMPOTENCY")
    assert [step["status"] for step in run["steps"]] == ["completed", "completed"]
    assert run["steps"][0]["prompt_tokens"] > 0
    assert run["steps"][0]["latency_ms"] >= 0

    fetched = await client.get(f"/v1/runs/{run['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["output"] == run["output"]


async def test_idempotency_returns_original_run(client: AsyncClient) -> None:
    workflow = await create_workflow(client)
    url = f"/v1/workflows/{workflow['id']}/runs"
    headers = {"Idempotency-Key": "same-request"}
    first = await client.post(url, json={"inputs": {"topic": "one"}}, headers=headers)
    second = await client.post(url, json={"inputs": {"topic": "two"}}, headers=headers)
    assert second.status_code == 201
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["inputs"] == {"topic": "one"}


async def test_deferred_run_is_executed_by_worker(client: AsyncClient) -> None:
    workflow = await create_workflow(client)
    created = await client.post(
        f"/v1/workflows/{workflow['id']}/runs",
        json={"inputs": {"topic": "workers"}},
        headers={"Prefer": "respond-async"},
    )

    assert created.status_code == 202
    assert created.json()["status"] == RunStatus.pending
    assert created.json()["steps"] == []
    assert created.json()["lease_owner"] is None

    processed = await run_worker_once(
        TestSession,
        Settings(app_env="test", worker_id="test-worker"),
    )

    assert processed is True
    fetched = await client.get(f"/v1/runs/{created.json()['id']}")
    assert fetched.json()["status"] == RunStatus.completed
    assert fetched.json()["output"].endswith("EXPLAIN WORKERS")
    assert fetched.json()["lease_owner"] is None
    assert fetched.json()["lease_expires_at"] is None
    assert fetched.json()["heartbeat_at"] is not None


async def test_worker_claims_and_renews_its_lease(client: AsyncClient) -> None:
    workflow = await create_workflow(client)
    created = await client.post(
        f"/v1/workflows/{workflow['id']}/runs",
        json={"inputs": {}},
        headers={"Prefer": "respond-async"},
    )
    claimed_at = datetime(2026, 9, 24, tzinfo=UTC)

    async with TestSession() as session:
        claimed = await claim_next_run(
            session,
            "worker-a",
            60,
            now=claimed_at,
        )
        assert claimed is not None
        assert claimed.id == created.json()["id"]
        assert claimed.lease_owner == "worker-a"
        assert claimed.heartbeat_at == claimed_at
        assert claimed.lease_expires_at == claimed_at + timedelta(seconds=60)

    renewed_at = claimed_at + timedelta(seconds=15)
    async with TestSession() as session:
        assert not await renew_lease(session, claimed.id, "worker-b", 60, now=renewed_at)
        assert await renew_lease(session, claimed.id, "worker-a", 60, now=renewed_at)
        renewed = await session.get(type(claimed), claimed.id)
        assert renewed is not None
        assert renewed.heartbeat_at is not None
        assert renewed.lease_expires_at is not None
        assert renewed.heartbeat_at.replace(tzinfo=UTC) == renewed_at
        assert renewed.lease_expires_at.replace(tzinfo=UTC) == renewed_at + timedelta(seconds=60)
        assert not await renew_lease(
            session,
            claimed.id,
            "worker-a",
            60,
            now=renewed_at + timedelta(seconds=61),
        )


async def test_worker_fails_expired_run_without_replaying_steps(client: AsyncClient) -> None:
    workflow = await create_workflow(client)
    created = await client.post(
        f"/v1/workflows/{workflow['id']}/runs",
        json={"inputs": {"topic": "recovery"}},
        headers={"Prefer": "respond-async"},
    )
    recovered_at = datetime(2026, 9, 25, tzinfo=UTC)

    async with TestSession() as session:
        claimed = await claim_next_run(
            session,
            "dead-worker",
            60,
            now=recovered_at - timedelta(seconds=120),
        )
        assert claimed is not None
        step = StepRun(
            run_id=claimed.id,
            step_key="draft",
            position=0,
            step_type="llm",
            status=RunStatus.running,
        )
        session.add(step)
        await session.commit()

    async with TestSession() as session:
        assert await recover_expired_runs(session, 100, now=recovered_at) == 1
        recovered = await session.get(WorkflowRun, created.json()["id"])
        interrupted_step = await session.get(StepRun, step.id)
        assert recovered is not None
        assert interrupted_step is not None
        assert recovered.status == RunStatus.failed
        assert recovered.error == LEASE_EXPIRED_ERROR
        assert recovered.completed_at is not None
        assert recovered.lease_owner is None
        assert recovered.lease_expires_at is None
        assert interrupted_step.status == RunStatus.failed
        assert interrupted_step.error == LEASE_EXPIRED_ERROR
        assert interrupted_step.output is None
        with pytest.raises(WorkerLeaseLostError, match="no longer owns"):
            await assert_active_lease(
                session,
                recovered.id,
                "dead-worker",
                now=recovered_at,
            )
        assert await recover_expired_runs(session, 100, now=recovered_at) == 0


async def test_worker_reports_when_queue_is_empty() -> None:
    processed = await run_worker_once(TestSession, Settings(app_env="test"))
    assert processed is False


def test_worker_heartbeat_must_precede_lease_expiry() -> None:
    with pytest.raises(ValueError, match="heartbeat interval"):
        Settings(worker_lease_seconds=5, worker_heartbeat_seconds=5)


async def test_validation_and_not_found_responses(client: AsyncClient) -> None:
    invalid = await client.post(
        "/v1/workflows",
        json={
            "name": "Invalid",
            "definition": {"steps": [{"key": "bad", "type": "tool", "input": "x"}]},
        },
    )
    missing = await client.get("/v1/runs/not-found")
    assert invalid.status_code == 422
    assert missing.status_code == 404
