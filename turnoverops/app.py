import logging
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from turnoverops.api.routes import (
    activity_router,
    assignment_router,
    auth_router,
    cleaner_router,
    dashboard_router,
    dev_router,
    health_router,
    integration_router,
    notification_router,
    onboarding_router,
    property_router,
    reservation_router,
    scheduling_router,
    task_router,
)
from turnoverops.core.config import Settings, get_settings
from turnoverops.core.context import set_correlation_id
from turnoverops.core.db import make_engine, make_session_factory
from turnoverops.core.errors import AppError
from turnoverops.workers.jobs import start_scheduler

logger = logging.getLogger("turnoverops")


def _configure_logging() -> None:
    if logger.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler = None
    if app.state.settings.enable_scheduler:
        scheduler = start_scheduler(app.state.session_factory, app.state.settings)
    yield
    if scheduler is not None:
        scheduler.shutdown(wait=False)


def create_app(settings: Settings | None = None) -> FastAPI:
    _configure_logging()
    settings = settings or get_settings()
    engine = make_engine(settings.database_url)
    app = FastAPI(
        title="TurnoverOps",
        version="0.1.0",
        description=(
            "Host API for reservation import, cleaning tasks, and confirmation-based assignment. "
            "A proposed cleaner is not confirmed until they accept."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.frontend_origin],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "X-Correlation-Id"],
    )

    @app.middleware("http")
    async def correlation(request: Request, call_next):
        correlation_id = request.headers.get("x-correlation-id") or str(uuid4())
        set_correlation_id(correlation_id)
        try:
            response = await call_next(request)
        finally:
            set_correlation_id(None)
        response.headers["x-correlation-id"] = correlation_id
        return response

    @app.exception_handler(AppError)
    async def app_error(_request: Request, exc: AppError):
        body: dict = {"error": {"code": exc.code, "message": exc.message}}
        if exc.details is not None:
            body["error"]["details"] = exc.details
        return JSONResponse(status_code=exc.status_code, content=body)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={"error": {"code": "validation_error", "message": "Request validation failed", "details": exc.errors()}},
        )

    @app.exception_handler(Exception)
    async def unexpected(_request: Request, exc: Exception):
        if isinstance(exc, HTTPException):
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
        logger.exception("request_failed")
        return JSONResponse(status_code=500, content={"error": {"code": "internal_error", "message": "Something went wrong"}})

    for router in (
        health_router,
        auth_router,
        onboarding_router,
        property_router,
        cleaner_router,
        reservation_router,
        task_router,
        scheduling_router,
        assignment_router,
        dashboard_router,
        activity_router,
        notification_router,
        integration_router,
    ):
        app.include_router(router)
    if settings.dev_mode:
        app.include_router(dev_router)
    return app
