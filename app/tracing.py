from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter

from app.config import Settings


def create_tracer_provider(
    settings: Settings,
    *,
    exporter: SpanExporter | None = None,
) -> TracerProvider | None:
    """Build a provider only when OTLP export is explicitly enabled."""
    endpoint = settings.otel_exporter_otlp_traces_endpoint
    if endpoint is None:
        return None

    selected_exporter = exporter or OTLPSpanExporter(endpoint=endpoint)
    provider = TracerProvider(resource=Resource.create({SERVICE_NAME: settings.otel_service_name}))
    provider.add_span_processor(BatchSpanProcessor(selected_exporter))
    return provider


def configure_tracing(settings: Settings) -> TracerProvider | None:
    provider = create_tracer_provider(settings)
    if provider is not None:
        trace.set_tracer_provider(provider)
    return provider


def instrument_fastapi(app: FastAPI, provider: TracerProvider | None) -> None:
    if provider is not None:
        FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)
