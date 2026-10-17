"""Public cleaner links and rejection pages."""

from datetime import UTC, datetime

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from turnoverops.api.deps import AppSettings, Db
from turnoverops.services.cleaner_calendar import authorization_url, calendar_configured, finish_authorization
from turnoverops.services.cleaner_mail import rejection_confirm, rejection_details, rejection_preview

router = APIRouter(tags=["rejection"])


@router.get("/public/cleaner-calendar/connect/{token}")
def connect_cleaner_calendar(token: str, settings: AppSettings, db: Db) -> Response:
    if not calendar_configured(settings):
        return HTMLResponse("<h1>Calendar connection is not configured.</h1>", status_code=503)
    url = authorization_url(db, token, settings, datetime.now(UTC))
    if url is None:
        return HTMLResponse("<h1>This calendar connection link has expired or was already used.</h1>", status_code=410)
    return RedirectResponse(url, status_code=302)


@router.get("/public/cleaner-calendar/callback", response_class=HTMLResponse)
def cleaner_calendar_callback(
    settings: AppSettings,
    db: Db,
    state: str | None = None,
    code: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    if not state:
        return HTMLResponse("<h1>Calendar connection could not be verified.</h1>", status_code=400)
    result = finish_authorization(
        db,
        state,
        code=code,
        error=error,
        settings=settings,
        now=datetime.now(UTC),
    )
    content = {
        "connected": "Google Calendar is connected. You can close this page.",
        "skipped": "Google Calendar was not connected. Your configured availability remains in use.",
        "invalid": "This calendar connection link expired or was already used.",
        "error": "Google Calendar could not be connected. The owner can send a new connection email.",
    }.get(result, "Google Calendar could not be connected.")
    status = 200 if result in {"connected", "skipped"} else 400
    return HTMLResponse(f"<!doctype html><html><meta charset='utf-8'><title>TurnoverOps</title><body><h1>{content}</h1></body></html>", status_code=status)


@router.get("/public/rejections/{token}")
def get_rejection_details(token: str, settings: AppSettings, db: Db) -> dict:
    return rejection_details(db, token, settings, datetime.now(UTC))


@router.post("/public/rejections/{token}")
def submit_rejection(token: str, settings: AppSettings, db: Db) -> dict:
    _status, result = rejection_confirm(db, token, settings, datetime.now(UTC), as_json=True)
    return result


@router.get("/reject/{token}", response_class=HTMLResponse)
def preview_rejection(token: str, settings: AppSettings, db: Db) -> HTMLResponse:
    status, body = rejection_preview(db, token, settings, datetime.now(UTC))
    return HTMLResponse(body, status_code=status)


@router.post("/reject/{token}", response_class=HTMLResponse)
def confirm_rejection(token: str, settings: AppSettings, db: Db) -> HTMLResponse:
    status, body = rejection_confirm(db, token, settings, datetime.now(UTC))
    return HTMLResponse(body, status_code=status)
