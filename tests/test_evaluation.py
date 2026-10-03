import json
import sys
from io import StringIO
from pathlib import Path

import httpx
import pytest

from app.evaluation import (
    EvaluationCase,
    _canonical_json,
    _parser,
    _run_cli,
    evaluate_dataset,
    load_evaluation_dataset,
    main,
)


def test_canonical_json_is_order_independent_but_type_sensitive() -> None:
    assert _canonical_json({"a": 1, "b": 2}) == _canonical_json({"b": 2, "a": 1})
    assert _canonical_json(True) != _canonical_json(1)


def test_load_evaluation_dataset_validates_jsonl_and_unique_ids(tmp_path: Path) -> None:
    dataset = tmp_path / "evaluation.jsonl"
    dataset.write_text(
        '\n{"id":"object-output","inputs":{"topic":"reliability"},"expected_output":{"score":1}}\n',
        encoding="utf-8",
    )

    cases = load_evaluation_dataset(dataset)
    assert cases == [
        EvaluationCase(
            id="object-output",
            inputs={"topic": "reliability"},
            expected_output={"score": 1},
        )
    ]

    dataset.write_text(
        '{"id":"duplicate","expected_output":"one"}\n{"id":"duplicate","expected_output":"two"}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Duplicate evaluation case id on line 2"):
        load_evaluation_dataset(dataset)


async def test_evaluate_dataset_reports_exact_matches_and_failures() -> None:
    requests: list[dict[str, object]] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        payload: dict[str, object] = json.loads(request.content)
        requests.append(payload)
        topic = payload["inputs"]["topic"]  # type: ignore[index]
        return httpx.Response(
            201,
            json={
                "id": f"run-{topic}",
                "status": "completed",
                "output": {"answer": topic},
                "error": None,
            },
        )

    cases = [
        EvaluationCase(
            id="passes",
            inputs={"topic": "expected"},
            expected_output={"answer": "expected"},
        ),
        EvaluationCase(
            id="fails",
            inputs={"topic": "actual"},
            expected_output={"answer": "different"},
        ),
    ]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://runtime.test"
    ) as client:
        summary = await evaluate_dataset(client, "workflow-1", cases)

    assert requests == [
        {"inputs": {"topic": "expected"}},
        {"inputs": {"topic": "actual"}},
    ]
    assert summary.workflow_id == "workflow-1"
    assert summary.total == 2
    assert summary.passed == 1
    assert summary.failed == 1
    assert summary.cases[0].passed is True
    assert summary.cases[1].passed is False
    assert summary.cases[1].actual_output == {"answer": "actual"}
    assert summary.cases[1].error == "Output did not exactly match expected_output"


async def test_cli_prints_json_and_returns_one_for_a_mismatch(tmp_path: Path) -> None:
    dataset = tmp_path / "evaluation.jsonl"
    dataset.write_text(
        '{"id":"case-1","inputs":{"value":"actual"},"expected_output":"expected"}\n',
        encoding="utf-8",
    )

    async def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={"id": "run-1", "status": "completed", "output": "actual", "error": None},
        )

    args = _parser().parse_args(["workflow-1", str(dataset), "--api-url", "http://runtime.test"])
    output = StringIO()
    exit_code = await _run_cli(
        args,
        transport=httpx.MockTransport(respond),
        output=output,
    )

    assert exit_code == 1
    assert json.loads(output.getvalue())["failed"] == 1


def test_cli_returns_two_for_an_invalid_dataset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dataset = tmp_path / "invalid.jsonl"
    dataset.write_text("not-json\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["agent-evaluate", "workflow-1", str(dataset)])

    with pytest.raises(SystemExit) as raised:
        main()

    assert raised.value.code == 2
    assert "Invalid evaluation case on line 1" in capsys.readouterr().err
