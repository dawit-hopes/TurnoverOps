"""Public rejection pages. GET never changes an assignment."""

from datetime import UTC, datetime

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from turnoverops.api.deps import AppSettings, Db
from turnoverops.services.cleaner_mail import rejection_confirm, rejection_preview

router = APIRouter(tags=["rejection"])


@router.get("/reject/{token}", response_class=HTMLResponse)
def preview_rejection(token: str, settings: AppSettings, db: Db) -> HTMLResponse:
    status, body = rejection_preview(db, token, settings, datetime.now(UTC))
    return HTMLResponse(body, status_code=status)


@router.post("/reject/{token}", response_class=HTMLResponse)
def confirm_rejection(token: str, settings: AppSettings, db: Db) -> HTMLResponse:
    status, body = rejection_confirm(db, token, settings, datetime.now(UTC))
    return HTMLResponse(body, status_code=status)
