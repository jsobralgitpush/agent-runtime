from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

from app.config import Settings
from app.engine import WorkerLeaseLostError, assert_active_lease
from app.metrics import METRICS_CONTENT_TYPE
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


async def create_approval_workflow(client: AsyncClient) -> dict[str, object]:
    response = await client.post(
        "/v1/workflows",
        json={
            "name": "Reviewed publisher",
            "definition": {
                "steps": [
                    {
                        "key": "prepare",
                        "type": "tool",
                        "tool": "echo",
                        "input": "${input.message}",
                    },
                    {
                        "key": "review",
                        "type": "approval",
                        "depends_on": ["prepare"],
                        "input": {
                            "candidate": "${steps.prepare.output}",
                            "operation_key": "${runtime.operation_key}",
                        },
                    },
                    {
                        "key": "publish",
                        "type": "tool",
                        "tool": "uppercase",
                        "depends_on": ["review"],
                        "input": "${steps.prepare.output}",
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


async def test_provider_discovery_lists_available_names_without_configuration(
    client: AsyncClient,
) -> None:
    response = await client.get("/v1/providers")

    assert response.status_code == 200
    providers = response.json()
    names = [provider["name"] for provider in providers]
    assert names == sorted(names)
    assert "fake" in names
    assert all(set(provider) == {"name"} for provider in providers)


async def test_tool_discovery_lists_allowlisted_names_only(client: AsyncClient) -> None:
    response = await client.get("/v1/tools")

    assert response.status_code == 200
    assert response.json() == [
        {"name": "echo"},
        {"name": "extract_field"},
        {"name": "uppercase"},
    ]


async def test_workflow_creation_rejects_unknown_extensions_before_persisting(
    client: AsyncClient,
) -> None:
    response = await client.post(
        "/v1/workflows",
        json={
            "name": "Invalid extensions",
            "definition": {
                "steps": [
                    {
                        "key": "draft",
                        "type": "llm",
                        "provider": "missing-primary",
                        "fallback_providers": ["missing-fallback"],
                    },
                    {
                        "key": "publish",
                        "type": "tool",
                        "tool": "missing-tool",
                    },
                ]
            },
        },
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": {
            "code": "unknown_workflow_extensions",
            "unknown_providers": ["missing-fallback", "missing-primary"],
            "unknown_tools": ["missing-tool"],
        }
    }
    workflows = await client.get("/v1/workflows")
    assert workflows.json() == []


async def test_prometheus_metrics_use_bounded_route_labels(client: AsyncClient) -> None:
    await client.get("/v1/health")
    await client.get("/v1/runs/missing-run")
    await client.get("/v1/not-a-real-route/123")
    await client.request("CUSTOM", "/v1/health")

    response = await client.get("/v1/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"] == METRICS_CONTENT_TYPE
    assert (
        'agent_runtime_http_requests_total{method="GET",route="/v1/health",status_code="200"}'
        in response.text
    )
    assert (
        'agent_runtime_http_requests_total{method="GET",route="/v1/runs/{run_id}",'
        'status_code="404"}' in response.text
    )
    assert "agent_runtime_http_request_duration_seconds_bucket" in response.text
    assert "agent_runtime_http_requests_in_progress" in response.text
    assert (
        'agent_runtime_http_requests_total{method="GET",route="unmatched",status_code="404"}'
        in response.text
    )
    assert (
        'agent_runtime_http_requests_total{method="OTHER",route="/v1/health",status_code="405"}'
        in response.text
    )
    assert "missing-run" not in response.text
    assert "not-a-real-route" not in response.text
    assert 'route="/v1/metrics"' not in response.text
    assert "agent_runtime_queue_depth 0.0" in response.text
    assert "agent_runtime_queue_oldest_pending_age_seconds 0.0" in response.text


async def test_prometheus_metrics_report_database_queue_health(client: AsyncClient) -> None:
    workflow = await create_workflow(client)
    run_url = f"/v1/workflows/{workflow['id']}/runs"
    first = await client.post(run_url, json={"inputs": {}}, headers={"Prefer": "respond-async"})
    second = await client.post(run_url, json={"inputs": {}}, headers={"Prefer": "respond-async"})
    observed_at = datetime.now(UTC)

    async with TestSession() as session:
        first_run = await session.get(WorkflowRun, first.json()["id"])
        second_run = await session.get(WorkflowRun, second.json()["id"])
        assert first_run is not None
        assert second_run is not None
        first_run.created_at = observed_at - timedelta(seconds=90)
        second_run.created_at = observed_at - timedelta(seconds=20)
        await session.commit()

    metrics = (await client.get("/v1/metrics")).text
    assert "agent_runtime_queue_depth 2.0" in metrics
    age_line = next(
        line
        for line in metrics.splitlines()
        if line.startswith("agent_runtime_queue_oldest_pending_age_seconds ")
    )
    age_seconds = float(age_line.rsplit(" ", 1)[1])
    assert 90 <= age_seconds < 95


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
    assert run["steps"][0]["provider"] == "fake"
    assert run["steps"][0]["latency_ms"] >= 0

    fetched = await client.get(f"/v1/runs/{run['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["output"] == run["output"]

    metrics = (await client.get("/v1/metrics")).text
    assert (
        'agent_runtime_workflow_executions_total{mode="synchronous",status="completed"}' in metrics
    )
    assert (
        "agent_runtime_workflow_execution_duration_seconds_count{"
        'mode="synchronous",status="completed"}' in metrics
    )
    assert 'agent_runtime_workflow_step_attempts_total{result="success",step_type="llm"}' in metrics
    assert (
        'agent_runtime_workflow_step_attempts_total{result="success",step_type="tool"}' in metrics
    )
    assert run["id"] not in metrics


async def test_approval_pauses_and_resumes_without_replaying_completed_steps(
    client: AsyncClient,
) -> None:
    workflow = await create_approval_workflow(client)
    created = await client.post(
        f"/v1/workflows/{workflow['id']}/runs",
        json={"inputs": {"message": "ship it"}},
    )

    assert created.status_code == 201
    waiting = created.json()
    assert waiting["status"] == RunStatus.waiting
    assert waiting["completed_at"] is None
    assert waiting["output"] is None
    assert waiting["lease_owner"] is None
    assert [(step["step_key"], step["status"]) for step in waiting["steps"]] == [
        ("prepare", "completed"),
        ("review", "waiting"),
    ]
    assert waiting["steps"][1]["input"] == {
        "candidate": "ship it",
        "operation_key": f"{waiting['id']}:review",
    }

    approved = await client.post(
        f"/v1/runs/{waiting['id']}/approval",
        json={"decision": "approve", "comment": "Reviewed by release manager"},
    )

    assert approved.status_code == 200
    completed = approved.json()
    assert completed["status"] == RunStatus.completed
    assert completed["output"] == "SHIP IT"
    assert [step["step_key"] for step in completed["steps"]] == [
        "prepare",
        "review",
        "publish",
    ]
    assert completed["steps"][1]["attempts"] == 0
    assert completed["steps"][1]["output"] == {
        "decision": "approve",
        "comment": "Reviewed by release manager",
    }

    repeated = await client.post(
        f"/v1/runs/{waiting['id']}/approval",
        json={"decision": "approve"},
    )
    assert repeated.status_code == 409


async def test_rejected_approval_fails_run_without_executing_later_steps(
    client: AsyncClient,
) -> None:
    workflow = await create_approval_workflow(client)
    created = await client.post(
        f"/v1/workflows/{workflow['id']}/runs",
        json={"inputs": {"message": "do not ship"}},
    )
    run_id = created.json()["id"]

    rejected = await client.post(
        f"/v1/runs/{run_id}/approval",
        json={"decision": "reject", "comment": "Needs security review"},
        headers={"Prefer": "respond-async"},
    )

    assert rejected.status_code == 200
    run = rejected.json()
    assert run["status"] == RunStatus.failed
    assert run["error"] == "HumanApprovalRejected: approval was rejected"
    assert [step["step_key"] for step in run["steps"]] == ["prepare", "review"]
    assert run["steps"][1]["status"] == RunStatus.failed
    assert run["steps"][1]["output"] == {
        "decision": "reject",
        "comment": "Needs security review",
    }


async def test_worker_releases_lease_and_resumes_approved_run(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enqueue_context = {"traceparent": "00-11111111111111111111111111111111-2222222222222222-01"}
    continuation_context = {
        "traceparent": "00-33333333333333333333333333333333-4444444444444444-01"
    }
    current_context = enqueue_context
    monkeypatch.setattr("app.api.capture_trace_context", lambda: current_context)
    workflow = await create_approval_workflow(client)
    created = await client.post(
        f"/v1/workflows/{workflow['id']}/runs",
        json={"inputs": {"message": "queued"}},
        headers={"Prefer": "respond-async"},
    )

    async with TestSession() as session:
        queued = await session.get(WorkflowRun, created.json()["id"])
        assert queued is not None
        assert queued.trace_context == enqueue_context

    assert await run_worker_once(
        TestSession,
        Settings(app_env="test", worker_id="approval-worker"),
    )
    fetched = await client.get(f"/v1/runs/{created.json()['id']}")
    run = fetched.json()
    assert "trace_context" not in run
    assert run["status"] == RunStatus.waiting
    assert run["lease_owner"] is None
    assert run["lease_expires_at"] is None
    assert [step["status"] for step in run["steps"]] == ["completed", "waiting"]

    current_context = continuation_context
    approved = await client.post(
        f"/v1/runs/{run['id']}/approval",
        json={"decision": "approve", "comment": "Queue the continuation"},
        headers={"Prefer": "Respond-Async"},
    )
    assert approved.status_code == 202
    assert approved.json()["status"] == RunStatus.pending
    assert "trace_context" not in approved.json()
    assert [step["status"] for step in approved.json()["steps"]] == [
        "completed",
        "completed",
    ]

    async with TestSession() as session:
        persisted = await session.get(WorkflowRun, run["id"])
        assert persisted is not None
        assert persisted.trace_context == continuation_context

    assert await run_worker_once(
        TestSession,
        Settings(app_env="test", worker_id="resume-worker"),
    )
    completed = await client.get(f"/v1/runs/{run['id']}")
    resumed = completed.json()
    assert resumed["status"] == RunStatus.completed
    assert resumed["output"] == "QUEUED"
    assert resumed["lease_owner"] is None
    assert [step["step_key"] for step in resumed["steps"]] == [
        "prepare",
        "review",
        "publish",
    ]


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
        Settings(app_env="test", worker_id="test-worker", worker_metrics_port=9091),
    )

    assert processed is True
    fetched = await client.get(f"/v1/runs/{created.json()['id']}")
    assert fetched.json()["status"] == RunStatus.completed
    assert fetched.json()["output"].endswith("EXPLAIN WORKERS")
    assert fetched.json()["lease_owner"] is None
    assert fetched.json()["lease_expires_at"] is None
    assert fetched.json()["heartbeat_at"] is not None
    metrics = (await client.get("/v1/metrics")).text
    assert "agent_runtime_worker_claims_total" in metrics
    assert "agent_runtime_worker_active_runs 0.0" in metrics
    assert 'agent_runtime_workflow_executions_total{mode="worker",status="completed"}' in metrics


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


async def test_worker_requeues_and_resumes_explicitly_replay_safe_step(
    client: AsyncClient,
) -> None:
    workflow_response = await client.post(
        "/v1/workflows",
        json={
            "name": "Recoverable publisher",
            "definition": {
                "steps": [
                    {
                        "key": "prepare",
                        "type": "tool",
                        "tool": "echo",
                        "input": "draft",
                    },
                    {
                        "key": "publish",
                        "type": "tool",
                        "tool": "echo",
                        "depends_on": ["prepare"],
                        "input": {
                            "message": "${steps.prepare.output}",
                            "operation_key": "${runtime.operation_key}",
                        },
                        "replay_safe": True,
                    },
                ]
            },
        },
    )
    assert workflow_response.status_code == 201
    workflow = workflow_response.json()
    created = await client.post(
        f"/v1/workflows/{workflow['id']}/runs",
        json={"inputs": {}},
        headers={"Prefer": "respond-async"},
    )
    assert created.status_code == 202
    run_id = created.json()["id"]
    recovered_at = datetime(2026, 10, 10, tzinfo=UTC)

    async with TestSession() as session:
        claimed = await claim_next_run(
            session,
            "dead-worker",
            60,
            now=recovered_at - timedelta(seconds=120),
        )
        assert claimed is not None
        completed_step = StepRun(
            run_id=claimed.id,
            step_key="prepare",
            position=0,
            step_type="tool",
            status=RunStatus.completed,
            attempts=1,
            input="draft",
            output="draft",
            completed_at=recovered_at - timedelta(seconds=121),
        )
        interrupted_step = StepRun(
            run_id=claimed.id,
            step_key="publish",
            position=1,
            step_type="tool",
            status=RunStatus.running,
            attempts=1,
            input={"message": "draft", "operation_key": f"{run_id}:publish"},
        )
        session.add(completed_step)
        session.add(interrupted_step)
        await session.commit()

    async with TestSession() as session:
        assert await recover_expired_runs(session, 100, now=recovered_at) == 1
        recovered = await session.get(WorkflowRun, run_id)
        step = await session.get(StepRun, interrupted_step.id)
        assert recovered is not None
        assert step is not None
        assert recovered.status == RunStatus.pending
        assert recovered.error is None
        assert recovered.completed_at is None
        assert recovered.lease_owner is None
        assert recovered.lease_expires_at is None
        assert step.status == RunStatus.pending
        assert step.attempts == 1
        assert step.error is None

    assert await run_worker_once(TestSession, Settings(app_env="test")) is True
    completed = (await client.get(f"/v1/runs/{run_id}")).json()
    assert completed["status"] == "completed"
    assert completed["output"] == {
        "message": "draft",
        "operation_key": f"{run_id}:publish",
    }
    assert len(completed["steps"]) == 2
    assert completed["steps"][0]["attempts"] == 1
    assert completed["steps"][1]["attempts"] == 2
    assert completed["steps"][1]["input"] == {
        "message": "draft",
        "operation_key": f"{run_id}:publish",
    }


async def test_worker_requeues_expired_claim_before_any_step_started(
    client: AsyncClient,
) -> None:
    workflow = await create_workflow(client)
    created = await client.post(
        f"/v1/workflows/{workflow['id']}/runs",
        json={"inputs": {"topic": "recovery"}},
        headers={"Prefer": "respond-async"},
    )
    recovered_at = datetime(2026, 10, 10, tzinfo=UTC)

    async with TestSession() as session:
        claimed = await claim_next_run(
            session,
            "dead-before-start",
            60,
            now=recovered_at - timedelta(seconds=120),
        )
        assert claimed is not None

    async with TestSession() as session:
        assert await recover_expired_runs(session, 100, now=recovered_at) == 1
        recovered = await session.get(WorkflowRun, created.json()["id"])
        assert recovered is not None
        assert recovered.status == RunStatus.pending
        assert recovered.lease_owner is None
        assert recovered.lease_expires_at is None


async def test_worker_reports_when_queue_is_empty() -> None:
    processed = await run_worker_once(TestSession, Settings(app_env="test"))
    assert processed is False


def test_worker_heartbeat_must_precede_lease_expiry() -> None:
    with pytest.raises(ValueError, match="heartbeat interval"):
        Settings(worker_lease_seconds=5, worker_heartbeat_seconds=5)


def test_worker_metrics_port_is_bounded() -> None:
    with pytest.raises(ValueError, match="greater than or equal to 1"):
        Settings(worker_metrics_port=0)


async def test_approval_step_rejects_replay_safe_configuration(client: AsyncClient) -> None:
    response = await client.post(
        "/v1/workflows",
        json={
            "name": "Invalid replay policy",
            "definition": {
                "steps": [
                    {
                        "key": "review",
                        "type": "approval",
                        "replay_safe": True,
                    }
                ]
            },
        },
    )

    assert response.status_code == 422
    assert "replay safety" in response.text


async def test_validation_and_not_found_responses(client: AsyncClient) -> None:
    invalid_target = await client.post(
        "/v1/workflows",
        json={
            "name": "Invalid",
            "definition": {"steps": [{"key": "bad", "type": "tool", "input": "x"}]},
        },
    )
    invalid_budget = await client.post(
        "/v1/workflows",
        json={
            "name": "Invalid budget",
            "definition": {
                "max_runtime_seconds": 0,
                "steps": [{"key": "valid", "type": "llm", "input": "x"}],
            },
        },
    )
    invalid_token_budget = await client.post(
        "/v1/workflows",
        json={
            "name": "Invalid token budget",
            "definition": {
                "max_total_tokens": 0,
                "steps": [{"key": "valid", "type": "llm", "input": "x"}],
            },
        },
    )
    invalid_cost_budget = await client.post(
        "/v1/workflows",
        json={
            "name": "Invalid cost budget",
            "definition": {
                "max_estimated_cost_usd": 0,
                "steps": [{"key": "valid", "type": "llm", "input": "x"}],
            },
        },
    )
    invalid_dependency_graph = await client.post(
        "/v1/workflows",
        json={
            "name": "Invalid dependency graph",
            "definition": {
                "steps": [
                    {
                        "key": "first",
                        "type": "tool",
                        "tool": "echo",
                        "depends_on": ["second"],
                    },
                    {
                        "key": "second",
                        "type": "tool",
                        "tool": "echo",
                        "depends_on": ["first"],
                    },
                ]
            },
        },
    )
    invalid_parallel_workflow = await client.post(
        "/v1/workflows",
        json={
            "name": "Invalid parallel workflow",
            "definition": {
                "max_parallel_steps": 2,
                "steps": [{"key": "only", "type": "tool", "tool": "echo"}],
            },
        },
    )
    invalid_approval = await client.post(
        "/v1/workflows",
        json={
            "name": "Invalid approval",
            "definition": {
                "steps": [
                    {
                        "key": "review",
                        "type": "approval",
                        "provider": "fake",
                    }
                ]
            },
        },
    )
    missing = await client.get("/v1/runs/not-found")
    missing_approval = await client.post(
        "/v1/runs/not-found/approval",
        json={"decision": "approve"},
    )
    assert invalid_target.status_code == 422
    assert invalid_budget.status_code == 422
    assert invalid_token_budget.status_code == 422
    assert invalid_cost_budget.status_code == 422
    assert invalid_dependency_graph.status_code == 422
    assert invalid_parallel_workflow.status_code == 422
    assert invalid_approval.status_code == 422
    assert missing.status_code == 404
    assert missing_approval.status_code == 404
