import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TextIO

import httpx
from pydantic import BaseModel, Field, ValidationError

from app.models import RunStatus

MAX_EVALUATION_CASES = 1_000


class EvaluationCase(BaseModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,119}$")
    inputs: dict[str, Any] = Field(default_factory=dict)
    expected_output: Any


class EvaluationCaseResult(BaseModel):
    id: str
    passed: bool
    run_id: str
    expected_output: Any
    actual_output: Any | None = None
    error: str | None = None


class EvaluationSummary(BaseModel):
    workflow_id: str
    total: int
    passed: int
    failed: int
    cases: list[EvaluationCaseResult]


class _RunResponse(BaseModel):
    id: str
    status: RunStatus
    output: Any | None = None
    error: str | None = None


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def load_evaluation_dataset(path: Path) -> list[EvaluationCase]:
    cases: list[EvaluationCase] = []
    case_ids: set[str] = set()
    with path.open(encoding="utf-8") as dataset:
        for line_number, line in enumerate(dataset, start=1):
            if not line.strip():
                continue
            try:
                case = EvaluationCase.model_validate(json.loads(line))
            except (json.JSONDecodeError, ValidationError) as exc:
                raise ValueError(f"Invalid evaluation case on line {line_number}: {exc}") from exc
            if case.id in case_ids:
                raise ValueError(f"Duplicate evaluation case id on line {line_number}: {case.id}")
            cases.append(case)
            case_ids.add(case.id)
            if len(cases) > MAX_EVALUATION_CASES:
                raise ValueError(f"Evaluation datasets are limited to {MAX_EVALUATION_CASES} cases")
    if not cases:
        raise ValueError("Evaluation dataset must contain at least one case")
    return cases


async def evaluate_dataset(
    client: httpx.AsyncClient,
    workflow_id: str,
    cases: Sequence[EvaluationCase],
) -> EvaluationSummary:
    results: list[EvaluationCaseResult] = []
    for case in cases:
        response = await client.post(
            f"/v1/workflows/{workflow_id}/runs",
            json={"inputs": case.inputs},
        )
        response.raise_for_status()
        run = _RunResponse.model_validate(response.json())
        passed = run.status == RunStatus.completed and _canonical_json(
            run.output
        ) == _canonical_json(case.expected_output)
        if passed:
            error = None
        elif run.status != RunStatus.completed:
            error = run.error or f"Run finished with status {run.status.value}"
        else:
            error = "Output did not exactly match expected_output"
        results.append(
            EvaluationCaseResult(
                id=case.id,
                passed=passed,
                run_id=run.id,
                expected_output=case.expected_output,
                actual_output=run.output,
                error=error,
            )
        )

    passed_count = sum(result.passed for result in results)
    return EvaluationSummary(
        workflow_id=workflow_id,
        total=len(results),
        passed=passed_count,
        failed=len(results) - passed_count,
        cases=results,
    )


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a JSONL evaluation dataset against an Agent Runtime workflow."
    )
    parser.add_argument("workflow_id", help="ID of the persisted workflow to evaluate")
    parser.add_argument("dataset", type=Path, help="Path to the JSONL evaluation dataset")
    parser.add_argument("--api-url", default="http://localhost:8000", help="Agent Runtime base URL")
    parser.add_argument(
        "--timeout-seconds",
        type=_positive_float,
        default=300.0,
        help="HTTP timeout for each synchronous workflow run (default: 300)",
    )
    return parser


async def _run_cli(
    args: argparse.Namespace,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    output: TextIO = sys.stdout,
) -> int:
    cases = load_evaluation_dataset(args.dataset)
    async with httpx.AsyncClient(
        base_url=args.api_url.rstrip("/"),
        timeout=args.timeout_seconds,
        transport=transport,
    ) as client:
        summary = await evaluate_dataset(client, args.workflow_id, cases)
    print(summary.model_dump_json(indent=2), file=output)
    return 0 if summary.failed == 0 else 1


def main() -> None:
    args = _parser().parse_args()
    try:
        exit_code = asyncio.run(_run_cli(args))
    except (OSError, ValueError, httpx.HTTPError) as exc:
        print(f"Evaluation failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    raise SystemExit(exit_code)
