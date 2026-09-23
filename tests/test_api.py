from httpx import AsyncClient

from app.config import Settings
from app.models import RunStatus
from app.worker import run_worker_once
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

    processed = await run_worker_once(TestSession, Settings(app_env="test"))

    assert processed is True
    fetched = await client.get(f"/v1/runs/{created.json()['id']}")
    assert fetched.json()["status"] == RunStatus.completed
    assert fetched.json()["output"].endswith("EXPLAIN WORKERS")


async def test_worker_reports_when_queue_is_empty() -> None:
    processed = await run_worker_once(TestSession, Settings(app_env="test"))
    assert processed is False


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
