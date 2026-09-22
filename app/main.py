from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import router
from app.config import get_settings
from app.database import engine
from app.logging import configure_logging
from app.models import Base

settings = get_settings()
configure_logging(settings.log_level)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    if settings.app_env in {"development", "test"}:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
    yield


app = FastAPI(
    title="Agent Runtime",
    version="0.1.0",
    description="Durable, observable execution for sequential AI workflows.",
    lifespan=lifespan,
)
app.include_router(router, prefix="/v1")
