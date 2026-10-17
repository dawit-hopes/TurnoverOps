import logging
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from turnoverops.api.public import router as rejection_router
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
    planning_router,
    scheduling_router,
    settings_router,
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


def _public_validation_errors(errors: list) -> list[dict]:
    public: list[dict] = []
    for item in errors:
        row: dict = {}
        for key, value in item.items():
            if key == "input":
                continue
            if key == "ctx" and isinstance(value, dict):
                row[key] = {name: str(detail) for name, detail in value.items()}
            elif isinstance(value, (str, int, float, bool)) or value is None:
                row[key] = value
            elif isinstance(value, list):
                row[key] = [part if isinstance(part, (str, int)) else str(part) for part in value]
            else:
                row[key] = str(value)
        public.append(row)
    return public


def _validation_message(details: list[dict]) -> str:
    for item in details:
        message = str(item.get("msg") or "")
        if message.lower().startswith("value error, "):
            return message.split(", ", 1)[1]
        location = item.get("loc") or []
        field = str(location[-1]) if location else "field"
        if message.lower() == "field required":
            return f"Enter {_field_label(field)}."
    return "Check the form and try again."


def _field_label(field: str) -> str:
    labels = {
        "email": "an email address",
        "name": "a name",
        "ical_url": "a calendar address",
        "timezone": "a time zone",
    }
    return labels.get(field, field.replace("_", " "))


def create_app(settings: Settings | None = None) -> FastAPI:
    _configure_logging()
    settings = settings or get_settings()
    engine = make_engine(settings.database_url)
    app = FastAPI(
        title="TurnoverOps",
        version="0.1.0",
        description=(
            "Host API for reservation import, cleaning tasks, and confirmed assignments. "
            "A cleaner is assigned when the job is created and only needs to act if they cannot do it."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    origins = [item.strip() for item in settings.frontend_origin.split(",") if item.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
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
        details = _public_validation_errors(exc.errors())
        return JSONResponse(
            status_code=422,
            content={"error": {"code": "validation_error", "message": _validation_message(details), "details": details}},
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
        planning_router,
        settings_router,
        assignment_router,
        dashboard_router,
        activity_router,
        notification_router,
        integration_router,
        rejection_router,
    ):
        app.include_router(router)
    if settings.dev_mode:
        app.include_router(dev_router)
    return app
