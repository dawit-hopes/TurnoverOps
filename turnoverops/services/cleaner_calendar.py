"""Cleaner Google Calendar authorization and fail-closed availability checks."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from datetime import UTC, datetime, timedelta

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from turnoverops.core.config import Settings
from turnoverops.core.security import hash_token
from turnoverops.integrations import google_calendar_api
from turnoverops.integrations.google_calendar_api import CalendarError
from turnoverops.models import Cleaner
from turnoverops.scheduling.engine import BusyInterval

_OAUTH_STATE_TTL = timedelta(minutes=10)


def calendar_configured(settings: Settings) -> bool:
    return bool(
        settings.google_calendar_client_id
        and settings.google_calendar_client_secret
        and settings.cleaner_google_calendar_redirect_uri
    )


def issue_connect_link(db: Session, cleaner: Cleaner, settings: Settings, now: datetime) -> str | None:
    if not calendar_configured(settings):
        return None
    token = secrets.token_urlsafe(32)
    cleaner.calendar_connect_token_hash = hash_token(token)
    cleaner.calendar_connect_token_expires_at = now + timedelta(hours=settings.invite_ttl_hours)
    base_url = (settings.app_base_url or "http://localhost:8000").rstrip("/")
    return f"{base_url}/public/cleaner-calendar/connect/{token}"


def authorization_url(db: Session, token: str, settings: Settings, now: datetime) -> str | None:
    digest = hash_token(token)
    cleaner = db.scalar(select(Cleaner).where(Cleaner.calendar_connect_token_hash == digest))
    if (
        cleaner is None
        or not cleaner.active
        or cleaner.calendar_connect_token_expires_at is None
        or cleaner.calendar_connect_token_expires_at <= now
    ):
        return None
    state = secrets.token_urlsafe(32)
    cleaner.calendar_oauth_state_hash = hash_token(state)
    cleaner.calendar_oauth_state_expires_at = now + _OAUTH_STATE_TTL
    return google_calendar_api.cleaner_authorization_url(settings, state)


def _fernet(settings: Settings) -> Fernet:
    key = hashlib.sha256(f"turnoverops:cleaner-calendar:{settings.jwt_secret}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def _store_tokens(cleaner: Cleaner, token_data: dict, settings: Settings, *, previous: dict | None = None) -> None:
    tokens = {
        "access_token": token_data["access_token"],
        "refresh_token": token_data.get("refresh_token") or (previous or {}).get("refresh_token"),
        "expires_at": time.time() + int(token_data.get("expires_in") or 3600),
        "calendar_id": "primary",
    }
    cleaner.calendar_tokens_encrypted = _fernet(settings).encrypt(json.dumps(tokens).encode()).decode()


def _load_tokens(cleaner: Cleaner, settings: Settings) -> dict | None:
    if not cleaner.calendar_tokens_encrypted:
        return None
    try:
        payload = _fernet(settings).decrypt(cleaner.calendar_tokens_encrypted.encode())
        tokens = json.loads(payload)
    except (InvalidToken, ValueError, TypeError, json.JSONDecodeError):
        return None
    return tokens if isinstance(tokens, dict) else None


def finish_authorization(
    db: Session,
    state: str,
    *,
    code: str | None,
    error: str | None,
    settings: Settings,
    now: datetime,
) -> str:
    digest = hash_token(state)
    cleaner_id = db.scalar(
        select(Cleaner.id).where(
            Cleaner.calendar_oauth_state_hash == digest,
            Cleaner.calendar_oauth_state_expires_at > now,
            Cleaner.active.is_(True),
        )
    )
    if cleaner_id is None:
        return "invalid"
    result = db.execute(
        update(Cleaner)
        .where(
            Cleaner.id == cleaner_id,
            Cleaner.calendar_oauth_state_hash == digest,
            Cleaner.calendar_oauth_state_expires_at > now,
            Cleaner.active.is_(True),
        )
        .values(calendar_oauth_state_hash=None, calendar_oauth_state_expires_at=None)
    )
    if result.rowcount != 1:
        return "invalid"
    cleaner = db.get(Cleaner, cleaner_id)
    if cleaner is None:
        return "invalid"
    db.flush()
    if error or not code:
        return "skipped" if error == "access_denied" else "error"
    try:
        tokens = google_calendar_api.exchange_cleaner_code(settings, code)
        _store_tokens(cleaner, tokens, settings, previous=_load_tokens(cleaner, settings))
    except CalendarError:
        cleaner.calendar_status = "access_error"
        return "error"
    cleaner.calendar_status = "connected"
    cleaner.calendar_connect_token_hash = None
    cleaner.calendar_connect_token_expires_at = None
    return "connected"


def calendar_busy_intervals(
    db: Session,
    host_id: str,
    settings: Settings | None,
    start: datetime,
    end: datetime,
) -> list[BusyInterval]:
    cleaners = db.scalars(
        select(Cleaner).where(
            Cleaner.host_id == host_id,
            Cleaner.calendar_status.in_(("connected", "access_error")),
            Cleaner.active.is_(True),
        )
    ).all()
    intervals: list[BusyInterval] = []
    for cleaner in cleaners:
        tokens = _load_tokens(cleaner, settings) if settings is not None else None
        if tokens is None or settings is None or not calendar_configured(settings):
            cleaner.calendar_status = "access_error"
            continue
        access_token = tokens.get("access_token")
        if not access_token:
            cleaner.calendar_status = "access_error"
            continue
        if cleaner.calendar_status == "access_error" or float(tokens.get("expires_at") or 0) <= time.time() + 60:
            refresh_token = tokens.get("refresh_token")
            if not refresh_token:
                cleaner.calendar_status = "access_error"
                continue
            try:
                refreshed = google_calendar_api.refresh_access_token(settings, refresh_token)
            except CalendarError:
                cleaner.calendar_status = "access_error"
                continue
            _store_tokens(cleaner, refreshed, settings, previous=tokens)
            tokens = _load_tokens(cleaner, settings) or {}
            access_token = tokens.get("access_token")
        try:
            busy = google_calendar_api.freebusy(
                str(access_token),
                start,
                end,
                calendar_id=str(tokens.get("calendar_id") or "primary"),
            )
        except CalendarError:
            cleaner.calendar_status = "access_error"
            continue
        cleaner.calendar_status = "connected"
        for index, item in enumerate(busy):
            try:
                busy_start = datetime.fromisoformat(item["start"].replace("Z", "+00:00"))
                busy_end = datetime.fromisoformat(item["end"].replace("Z", "+00:00"))
            except (KeyError, TypeError, ValueError):
                cleaner.calendar_status = "access_error"
                intervals = [item for item in intervals if item.cleaner_id != cleaner.id]
                break
            if busy_start.tzinfo is None or busy_end.tzinfo is None or busy_end <= busy_start:
                cleaner.calendar_status = "access_error"
                intervals = [item for item in intervals if item.cleaner_id != cleaner.id]
                break
            intervals.append(
                BusyInterval(
                    cleaner_id=cleaner.id,
                    task_id=f"google-calendar:{cleaner.id}:{index}",
                    start=busy_start.astimezone(UTC),
                    end=busy_end.astimezone(UTC),
                    counts_toward_daily_limit=False,
                )
            )
    return intervals
