from datetime import UTC, datetime, time
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from turnoverops.core.config import Settings
from turnoverops.integrations.google_calendar_api import CalendarError
from turnoverops.models import Base, Cleaner, Host
from turnoverops.scheduling.engine import JobWindow, describe
from turnoverops.services.cleaner_calendar import (
    _load_tokens,
    _store_tokens,
    authorization_url,
    calendar_busy_intervals,
    finish_authorization,
    issue_connect_link,
)
from turnoverops.services.cleaner_mail import send_cleaner_welcome_email
from turnoverops.services.evaluation import evaluate_window


@pytest.fixture
def calendar_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        host = Host(id="host-1", google_sub="google-1", email="owner@example.com", name="Owner")
        db.add(host)
        db.flush()
        yield db, host
    engine.dispose()


@pytest.fixture
def calendar_settings():
    return Settings(
        _env_file=None,
        jwt_secret="calendar-test-secret-long-enough-for-encryption",
        google_calendar_client_id="calendar-client",
        google_calendar_client_secret="calendar-secret",
        cleaner_google_calendar_redirect_uri="http://localhost:8000/public/cleaner-calendar/callback",
        app_base_url="http://localhost:8000",
    )


def _cleaner(db, host, name="Cleaner"):
    row = Cleaner(
        id=f"cleaner-{name.lower()}",
        host_id=host.id,
        name=name,
        email=f"{name.lower()}@example.com",
        timezone="UTC",
        working_days=[0, 1, 2, 3, 4, 5, 6],
        work_start=time(9),
        work_end=time(17),
        max_jobs_per_day=None,
        active=True,
        calendar_status="not_connected",
    )
    db.add(row)
    db.flush()
    return row


def _calendar_token(url):
    return url.rstrip("/").rsplit("/", 1)[1]


def _oauth_state(db, cleaner, settings):
    link = issue_connect_link(db, cleaner, settings, datetime.now(UTC))
    auth_url = authorization_url(db, _calendar_token(link), settings, datetime.now(UTC))
    query = parse_qs(urlparse(auth_url).query)
    assert query["scope"] == ["https://www.googleapis.com/auth/calendar.freebusy"]
    return query["state"][0]


def _finish_connected(db, cleaner, settings, monkeypatch):
    state = _oauth_state(db, cleaner, settings)
    monkeypatch.setattr(
        "turnoverops.services.cleaner_calendar.google_calendar_api.exchange_cleaner_code",
        lambda settings, code: {"access_token": "access", "refresh_token": "refresh", "expires_in": 3600},
    )
    assert finish_authorization(
        db, state, code="oauth-code", error=None, settings=settings, now=datetime.now(UTC)
    ) == "connected"
    return state


def test_welcome_email_uses_secure_one_time_link_and_encrypted_tokens(calendar_db, calendar_settings, monkeypatch):
    db, host = calendar_db
    cleaner = _cleaner(db, host)
    sent = []
    monkeypatch.setattr("turnoverops.services.cleaner_mail.send_smtp_email", lambda settings, **mail: sent.append(mail))

    note = send_cleaner_welcome_email(db, cleaner, calendar_settings, datetime.now(UTC))
    assert note.status == "sent"
    assert "Connect Google Calendar" in sent[0]["html"]
    token = _calendar_token(sent[0]["text"].split("\n")[-1])
    auth_url = authorization_url(db, token, calendar_settings, datetime.now(UTC))
    state = parse_qs(urlparse(auth_url).query)["state"][0]
    monkeypatch.setattr(
        "turnoverops.services.cleaner_calendar.google_calendar_api.exchange_cleaner_code",
        lambda settings, code: {"access_token": "private-access", "refresh_token": "private-refresh", "expires_in": 3600},
    )
    result = finish_authorization(db, state, code="ok", error=None, settings=calendar_settings, now=datetime.now(UTC))
    assert result == "connected"
    assert cleaner.calendar_status == "connected"
    assert "private-access" not in cleaner.calendar_tokens_encrypted
    assert "private-refresh" not in cleaner.calendar_tokens_encrypted
    assert _load_tokens(cleaner, calendar_settings)["access_token"] == "private-access"
    assert finish_authorization(db, state, code="ok", error=None, settings=calendar_settings, now=datetime.now(UTC)) == "invalid"

    retry = send_cleaner_welcome_email(db, cleaner, calendar_settings, datetime.now(UTC))
    assert retry.id == note.id
    assert len(sent) == 1


def test_skipped_authorization_keeps_configured_schedule(calendar_db, calendar_settings):
    db, host = calendar_db
    cleaner = _cleaner(db, host)
    state = _oauth_state(db, cleaner, calendar_settings)
    result = finish_authorization(db, state, code=None, error="access_denied", settings=calendar_settings, now=datetime.now(UTC))
    assert result == "skipped"
    assert cleaner.calendar_status == "not_connected"

    decision = evaluate_window(
        db,
        host.id,
        JobWindow(
            property_id="property-1",
            property_timezone="UTC",
            window_start=datetime(2026, 11, 3, 9, tzinfo=UTC),
            window_end=datetime(2026, 11, 3, 12, tzinfo=UTC),
            duration_minutes=60,
        ),
        settings=calendar_settings,
    )
    assert decision.selected.cleaner_id == cleaner.id
    assert "external calendar availability unverified" in describe(decision.selected)


def test_unavailable_connected_calendar_fails_closed(calendar_db, calendar_settings, monkeypatch):
    db, host = calendar_db
    cleaner = _cleaner(db, host)
    _finish_connected(db, cleaner, calendar_settings, monkeypatch)
    monkeypatch.setattr(
        "turnoverops.services.cleaner_calendar.google_calendar_api.freebusy",
        lambda *args, **kwargs: (_ for _ in ()).throw(CalendarError("unavailable")),
    )
    decision = evaluate_window(
        db,
        host.id,
        JobWindow(
            property_id="property-1",
            property_timezone="UTC",
            window_start=datetime(2026, 11, 3, 9, tzinfo=UTC),
            window_end=datetime(2026, 11, 3, 12, tzinfo=UTC),
            duration_minutes=60,
        ),
        settings=calendar_settings,
    )
    assert cleaner.calendar_status == "access_error"
    assert decision.selected is None
    assert "calendar_unavailable" in decision.rejected[0].reasons


def test_freebusy_blocks_time_without_consuming_cleaning_capacity(calendar_db, calendar_settings, monkeypatch):
    db, host = calendar_db
    connected = _cleaner(db, host, "Connected")
    unconnected = _cleaner(db, host, "Unconnected")
    _finish_connected(db, connected, calendar_settings, monkeypatch)
    monkeypatch.setattr(
        "turnoverops.services.cleaner_calendar.google_calendar_api.freebusy",
        lambda *args, **kwargs: [{"start": "2026-11-03T09:00:00Z", "end": "2026-11-03T11:00:00Z"}],
    )
    window = JobWindow(
        property_id="property-1",
        property_timezone="UTC",
        window_start=datetime(2026, 11, 3, 9, tzinfo=UTC),
        window_end=datetime(2026, 11, 3, 12, tzinfo=UTC),
        duration_minutes=60,
    )
    decision = evaluate_window(db, host.id, window, settings=calendar_settings)
    by_id = {item.cleaner_id: item for item in decision.ranked_feasible}
    assert by_id[connected.id].scheduled_start == datetime(2026, 11, 3, 11, tzinfo=UTC)
    assert by_id[connected.id].jobs_that_day == 0
    assert by_id[unconnected.id].scheduled_start == datetime(2026, 11, 3, 9, tzinfo=UTC)
