import asyncio
import os
from contextlib import asynccontextmanager, suppress
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.core.config import settings
from app.core.historical_import_limits import MAX_HISTORICAL_IMPORT_REQUEST_BYTES
from app.core.logging import configure_logging
from app.database.session import engine
from app.middleware.historical_import_request_size import (
    HistoricalImportRequestSizeLimitMiddleware,
)
from app.runtime.automation_scheduler import run_forever as run_automation_scheduler

configure_logging()


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler_task: asyncio.Task[None] | None = None
    if _env_flag("AUTOMATION_SCHEDULER_ENABLED"):
        scheduler_task = asyncio.create_task(
            run_automation_scheduler(),
            name="tia-automation-scheduler",
        )
    try:
        yield
    finally:
        if scheduler_task is not None:
            scheduler_task.cancel()
            with suppress(asyncio.CancelledError):
                await scheduler_task
        engine.dispose()


docs_url = "/docs" if settings.docs_enabled else None
redoc_url = "/redoc" if settings.docs_enabled else None
openapi_url = "/openapi.json" if settings.docs_enabled else None

app = FastAPI(
    title=settings.app_name,
    debug=settings.debug,
    lifespan=lifespan,
    docs_url=docs_url,
    redoc_url=redoc_url,
    openapi_url=openapi_url,
)

if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

historical_import_preview_path = f"{settings.api_v1_prefix.rstrip('/')}/clinic/history/preview"
app.add_middleware(
    HistoricalImportRequestSizeLimitMiddleware,
    path=historical_import_preview_path,
    max_body_bytes=MAX_HISTORICAL_IMPORT_REQUEST_BYTES,
)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or str(uuid4())
    response = await call_next(request)
    response.headers["x-request-id"] = request_id
    return response


app.include_router(api_router, prefix=settings.api_v1_prefix)


@app.get("/", tags=["root"])
def root() -> dict[str, str | None]:
    return {
        "name": settings.app_name,
        "environment": settings.environment,
        "status": "running",
        "docs": docs_url,
        "liveness": f"{settings.api_v1_prefix}/health/live",
        "readiness": f"{settings.api_v1_prefix}/health/ready",
    }
