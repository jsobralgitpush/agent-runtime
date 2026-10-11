import time
from threading import Thread
from wsgiref.simple_server import WSGIServer

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    start_http_server,
)
from starlette.types import ASGIApp, Message, Receive, Scope, Send

API_PREFIX = "/v1"
METRICS_PATH = f"{API_PREFIX}/metrics"
METRICS_CONTENT_TYPE = CONTENT_TYPE_LATEST
KNOWN_HTTP_METHODS = frozenset(
    {"CONNECT", "DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT", "TRACE"}
)

registry = CollectorRegistry()
http_requests_total = Counter(
    "agent_runtime_http_requests_total",
    "Total HTTP requests handled by the API.",
    ("method", "route", "status_code"),
    registry=registry,
)
http_request_duration_seconds = Histogram(
    "agent_runtime_http_request_duration_seconds",
    "HTTP request duration in seconds.",
    ("method", "route", "status_code"),
    registry=registry,
)
http_requests_in_progress = Gauge(
    "agent_runtime_http_requests_in_progress",
    "HTTP requests currently being handled by the API.",
    ("method",),
    registry=registry,
)
workflow_executions_total = Counter(
    "agent_runtime_workflow_executions_total",
    "Workflow engine invocations by execution mode and resulting status.",
    ("mode", "status"),
    registry=registry,
)
workflow_execution_duration_seconds = Histogram(
    "agent_runtime_workflow_execution_duration_seconds",
    "Workflow engine invocation duration in seconds.",
    ("mode", "status"),
    registry=registry,
)
workflow_step_attempts_total = Counter(
    "agent_runtime_workflow_step_attempts_total",
    "Provider and tool step attempts by step type and result.",
    ("step_type", "result"),
    registry=registry,
)
worker_claims_total = Counter(
    "agent_runtime_worker_claims_total",
    "Workflow runs claimed by this worker process.",
    registry=registry,
)
worker_recoveries_total = Counter(
    "agent_runtime_worker_recoveries_total",
    "Expired workflow runs handled by recovery outcome.",
    ("outcome",),
    registry=registry,
)
worker_active_runs = Gauge(
    "agent_runtime_worker_active_runs",
    "Workflow runs currently executed by this worker process.",
    registry=registry,
)


def render_metrics() -> bytes:
    return generate_latest(registry)


def record_workflow_execution(mode: str, status: str, duration_seconds: float) -> None:
    labels = {"mode": mode, "status": status}
    workflow_executions_total.labels(**labels).inc()
    workflow_execution_duration_seconds.labels(**labels).observe(duration_seconds)


def record_step_attempt(step_type: str, result: str) -> None:
    workflow_step_attempts_total.labels(step_type=step_type, result=result).inc()


def start_worker_metrics_server(host: str, port: int) -> tuple[WSGIServer, Thread]:
    return start_http_server(port, addr=host, registry=registry)


class PrometheusMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") == METRICS_PATH:
            await self.app(scope, receive, send)
            return

        requested_method = str(scope.get("method", "")).upper()
        method = requested_method if requested_method in KNOWN_HTTP_METHODS else "OTHER"
        status_code = 500
        started = time.perf_counter()
        http_requests_in_progress.labels(method=method).inc()

        async def send_with_status(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_with_status)
        finally:
            route = scope.get("route")
            route_path = getattr(route, "path", None)
            if not isinstance(route_path, str):
                route_path = "unmatched"
            elif str(scope.get("path", "")).startswith(f"{API_PREFIX}/"):
                route_path = f"{API_PREFIX}{route_path}"
            labels = {
                "method": method,
                "route": route_path,
                "status_code": str(status_code),
            }
            http_requests_total.labels(**labels).inc()
            http_request_duration_seconds.labels(**labels).observe(time.perf_counter() - started)
            http_requests_in_progress.labels(method=method).dec()
