from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from opentelemetry.sdk.resources import SERVICE_NAME
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind
from pytest import MonkeyPatch

from app.config import Settings
from app.tracing import create_tracer_provider, instrument_fastapi


def test_tracing_is_disabled_without_an_otlp_endpoint() -> None:
    assert create_tracer_provider(Settings(app_env="test")) is None


async def test_fastapi_spans_are_exported_with_service_identity(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("OTEL_TRACES_SAMPLER", "always_on")
    exporter = InMemorySpanExporter()
    settings = Settings(
        app_env="test",
        otel_exporter_otlp_traces_endpoint="http://collector:4318/v1/traces",
        otel_service_name="agent-runtime-test",
    )
    provider = create_tracer_provider(settings, exporter=exporter)
    assert provider is not None
    test_app = FastAPI()

    @test_app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    instrument_fastapi(test_app, provider)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=test_app), base_url="http://test"
        ) as client:
            response = await client.get("/health")
        assert response.status_code == 200
        assert provider.force_flush()
        server_spans: list[ReadableSpan] = [
            span for span in exporter.get_finished_spans() if span.kind == SpanKind.SERVER
        ]
        assert len(server_spans) == 1
        assert server_spans[0].attributes is not None
        assert server_spans[0].attributes["http.route"] == "/health"
        assert server_spans[0].resource.attributes[SERVICE_NAME] == "agent-runtime-test"
    finally:
        provider.shutdown()
