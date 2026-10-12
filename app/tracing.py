from collections.abc import Iterator, Mapping
from contextlib import contextmanager

from fastapi import FastAPI
from opentelemetry import context, propagate, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter

from app.config import Settings

TRACE_CONTEXT_KEYS = frozenset({"traceparent", "tracestate"})


def capture_trace_context() -> dict[str, str] | None:
    """Serialize the current valid W3C trace context for deferred execution."""
    if not trace.get_current_span().get_span_context().is_valid:
        return None

    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    filtered = {
        key.lower(): value for key, value in carrier.items() if key.lower() in TRACE_CONTEXT_KEYS
    }
    return filtered or None


@contextmanager
def use_trace_context(carrier: Mapping[str, str] | None) -> Iterator[None]:
    """Attach a persisted W3C context while a deferred run is executed."""
    filtered = {
        key.lower(): value
        for key, value in (carrier or {}).items()
        if key.lower() in TRACE_CONTEXT_KEYS
    }
    if "traceparent" not in filtered:
        yield
        return

    extracted = propagate.extract(filtered)
    if not trace.get_current_span(extracted).get_span_context().is_valid:
        yield
        return

    token = context.attach(extracted)
    try:
        yield
    finally:
        context.detach(token)


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
