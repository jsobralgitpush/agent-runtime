from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import router
from app.config import get_settings
from app.database import engine
from app.logging import configure_logging
from app.metrics import API_PREFIX, PrometheusMiddleware
from app.models import Base
from app.tracing import configure_tracing, instrument_fastapi

settings = get_settings()
configure_logging(settings.log_level)
tracer_provider = configure_tracing(settings)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    try:
        if settings.app_env in {"development", "test"}:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
        yield
    finally:
        if tracer_provider is not None:
            tracer_provider.shutdown()


app = FastAPI(
    title="Agent Runtime",
    version="0.1.0",
    description="Durable, observable execution for sequential AI workflows.",
    lifespan=lifespan,
)
app.add_middleware(PrometheusMiddleware)
app.include_router(router, prefix=API_PREFIX)
instrument_fastapi(app, tracer_provider)
