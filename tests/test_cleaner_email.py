import smtplib
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

from turnoverops.core.config import Settings
from turnoverops.integrations.smtp_email import SmtpNotConfigured, validate_smtp_config
from turnoverops.models import Assignment, Cleaner, CleaningTask, Host, Notification, Property, RejectionToken
from turnoverops.services.cleaner_mail import (
    google_calendar_url,
    render_assignment_email,
    send_assignment_email,
    send_weekly_schedules,
)
from tests.conftest import STAYS, create_cleaner, create_property, list_tasks, login, sync

WEEK = [
    {"external_uid": "a", "start": "2026-11-01T12:00:00Z", "end": "2026-11-03T07:00:00Z"},
    {"external_uid": "b", "start": "2026-11-03T12:00:00Z", "end": "2026-11-05T07:00:00Z"},
    {"external_uid": "c", "start": "2026-11-05T12:00:00Z", "end": "2026-11-07T07:00:00Z"},
]


class Inbox:
    def __init__(self):
        self.messages: list[EmailMessage] = []
        self.fail: Exception | None = None
        self.starttls = 0

    def install(self, monkeypatch):
        inbox = self

        class FakeSMTP:
            def __init__(self, host, port, timeout=None):
                self.host = host
                self.port = port

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def ehlo(self):
                return (250, b"ok")

            def starttls(self, context=None):
                inbox.starttls += 1
                return (220, b"ready")

            def login(self, user, password):
                if isinstance(inbox.fail, smtplib.SMTPAuthenticationError):
                    raise inbox.fail
                return (235, b"ok")

            def send_message(self, message):
                if inbox.fail is not None and not isinstance(inbox.fail, smtplib.SMTPAuthenticationError):
                    raise inbox.fail
                inbox.messages.append(message)
                return {}

        monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
        return self


def _smtp(client):
    settings = client.app.state.settings
    settings.smtp_host = "smtp.gmail.com"
    settings.smtp_port = 587
    settings.smtp_username = "bot@example.com"
    settings.smtp_password = "app-password"
    settings.email_from = "bot@example.com"
    settings.app_base_url = "http://localhost:8000"
    settings.app_timezone = "Africa/Addis_Ababa"
    return settings


def _plain(message: EmailMessage) -> str:
    return message.get_body(preferencelist=("plain",)).get_content()


def _subjects(inbox: Inbox) -> list[str]:
    return [message["Subject"] for message in inbox.messages]


def _link(text: str) -> str:
    for line in text.splitlines():
        if "/reject/" in line:
            return line.strip()
    raise AssertionError("reject link missing")


def _token(text: str) -> str:
    return _link(text).rstrip("/").split("/")[-1]


def _task_token(client, task_id: str) -> str:
    db = client.app.state.session_factory()
    try:
        note = (
            db.query(Notification)
            .filter(Notification.task_id == task_id, Notification.message_type == "assignment_email")
            .order_by(Notification.created_at.desc())
            .first()
        )
        assert note is not None
        return _token(note.body)
    finally:
        db.close()


def _wide(client, headers, prop, name):
    return create_cleaner(
        client,
        headers,
        name,
        timezone="Africa/Addis_Ababa",
        work_start="00:00:00",
        work_end="23:00:00",
        preferred_property_ids=[prop["id"]],
    )


def _one_job(client, monkeypatch, *, cleaners=2, address="12 Bole Road, Addis Ababa"):
    inbox = Inbox().install(monkeypatch)
    _smtp(client)
    headers = login(client)
    prop = create_property(
        client,
        headers,
        name="Harbor & Co",
        address=address,
        timezone="Africa/Addis_Ababa",
        cleaning_duration_minutes=60,
    )
    ada = _wide(client, headers, prop, "Ada")
    boa = _wide(client, headers, prop, "Boa") if cleaners > 1 else None
    sync(client, headers, prop["id"], STAYS)
    task = next(item for item in list_tasks(client, headers, prop["id"]) if item["assignment"])
    return inbox, headers, prop, ada, boa, task


def test_missing_smtp_config_is_reported_and_does_not_open_a_connection(client, monkeypatch):
    called = {"smtp": False}

    class Boom:
        def __init__(self, *args, **kwargs):
            called["smtp"] = True

    monkeypatch.setattr("smtplib.SMTP", Boom)
    bare = Settings(_env_file=None, smtp_username="", smtp_password="", email_from="")
    try:
        validate_smtp_config(bare)
    except SmtpNotConfigured as exc:
        assert "SMTP_USERNAME" in str(exc)
        assert "SMTP_PASSWORD" in str(exc)
        assert "EMAIL_FROM" in str(exc)
    else:
        raise AssertionError("missing SMTP settings should fail validation")
    headers = login(client)
    prop = create_property(client, headers, timezone="Africa/Addis_Ababa")
    create_cleaner(client, headers, "Ada", work_start="00:00:00", work_end="23:00:00", preferred_property_ids=[prop["id"]])
    sync(client, headers, prop["id"], STAYS)
    task = next(item for item in list_tasks(client, headers, prop["id"]) if item["assignment"])
    assert task["assignment_email_status"] == "not_configured"
    assert called["smtp"] is False
    assert task["status"] == "confirmed"


def test_successful_send_and_smtp_failure(client, monkeypatch):
    inbox = Inbox().install(monkeypatch)
    _smtp(client)
    headers = login(client)
    prop = create_property(client, headers, name="Harbor & Co", address="12 Bole Road", timezone="UTC")
    create_cleaner(client, headers, "Ada", work_start="00:00:00", work_end="23:00:00", preferred_property_ids=[prop["id"]])
    sync(client, headers, prop["id"], STAYS)
    synced = _assignment_count(client)
    assert inbox.starttls >= 1
    message = next(item for item in inbox.messages if item["Subject"].startswith("Cleaning Assignment — Harbor & Co —"))
    text = _plain(message)
    assert "already confirmed" in text
    assert "Accept" not in text
    assert "12 Bole Road" in text
    assert "https://calendar.google.com/calendar/render?" in text
    assert "/reject/" in text
    task = next(item for item in list_tasks(client, headers, prop["id"]) if item["assignment"])
    assert task["assignment_email_status"] == "sent"

    inbox.fail = smtplib.SMTPServerDisconnected("temporary outage")
    inbox.messages.clear()
    db = client.app.state.session_factory()
    try:
        assignment = db.get(Assignment, task["assignment"]["id"])
        assignment.scheduled_start = assignment.scheduled_start + timedelta(minutes=30)
        db.commit()
        send_assignment_email(db, assignment, client.app.state.settings, datetime.now(UTC))
        db.commit()
        note = db.query(Notification).filter(Notification.dedupe_key.contains(assignment.id), Notification.status == "failed").one()
        assert note.next_retry_at is not None
        assert note.retry_count == 1
        assert _assignment_count(client) == synced
    finally:
        db.close()

    inbox.fail = smtplib.SMTPAuthenticationError(535, b"bad credentials")
    db = client.app.state.session_factory()
    try:
        assignment = db.get(Assignment, task["assignment"]["id"])
        assignment.scheduled_end = assignment.scheduled_end + timedelta(minutes=15)
        db.commit()
        send_assignment_email(db, assignment, client.app.state.settings, datetime.now(UTC))
        db.commit()
        notes = db.query(Notification).filter(Notification.status == "failed").all()
        assert any(note.next_retry_at is None for note in notes)
    finally:
        db.close()


def test_calendar_url_encoding_and_addis_display(client, monkeypatch):
    url = google_calendar_url(
        title="A & B",
        start=datetime(2026, 11, 3, 6, 0, tzinfo=UTC),
        end=datetime(2026, 11, 3, 7, 0, tzinfo=UTC),
        location="12 Bole Road",
        details="Line & detail",
        timezone="Africa/Addis_Ababa",
    )
    assert url.startswith("https://calendar.google.com/calendar/render?")
    assert "text=A%20%26%20B" in url
    assert "dates=20261103T090000%2F20261103T100000" in url
    assert "ctz=Africa%2FAddis_Ababa" in url
    assert "location=12%20Bole%20Road" in url
    assert "+" not in url.split("?", 1)[1]

    inbox, headers, prop, _ada, _boa, task = _one_job(client, monkeypatch)
    db = client.app.state.session_factory()
    try:
        assignment = db.get(Assignment, task["assignment"]["id"])
        row = db.get(CleaningTask, task["id"])
        cleaner = db.get(Cleaner, assignment.cleaner_id)
        property_row = db.get(Property, prop["id"])
        assignment.scheduled_start = datetime(2026, 11, 3, 6, 0, tzinfo=UTC)
        assignment.scheduled_end = datetime(2026, 11, 3, 7, 0, tzinfo=UTC)
        row.window_end = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
        row.instructions = "Use the side gate"
        subject, text, html = render_assignment_email(cleaner, property_row, row, assignment, "token", client.app.state.settings)
    finally:
        db.close()
    assert subject == "Cleaning Assignment — Harbor & Co — 3 Nov 2026"
    assert "9:00 AM" in text
    assert "10:00 AM" in text
    assert "3:00 PM" in text
    assert "Addis Ababa time" in text
    assert "Use the side gate" in text
    assert "Addis Ababa" in text
    assert "Accept" not in html
    assert inbox.messages


def test_weekly_email_groups_days_and_skips_per_job_duplicates(client, monkeypatch):
    inbox = Inbox().install(monkeypatch)
    _smtp(client)
    headers = login(client)
    prop = create_property(client, headers, name="Harbor", address="12 Bole Road", timezone="Africa/Addis_Ababa")
    ada = _wide(client, headers, prop, "Ada")
    sync(client, headers, prop["id"], WEEK)
    inbox.messages.clear()
    db = client.app.state.session_factory()
    try:
        db.query(Notification).delete()
        db.commit()
        moment = datetime(2026, 11, 4, 9, 0, tzinfo=UTC)
        results = send_weekly_schedules(db, client.app.state.settings, moment, host_id=_host_id(db))
        db.commit()
    finally:
        db.close()
    assert len(results) == 1
    assert results[0].status == "sent"
    assert len(inbox.messages) == 1
    text = _plain(inbox.messages[0])
    assert inbox.messages[0]["Subject"].startswith("Cleaning schedule —")
    assert "Tuesday, 3 November 2026" in text
    assert "Thursday, 5 November 2026" in text
    assert "Wednesday, 4 November 2026" not in text
    assert text.index("Tuesday, 3 November 2026") < text.index("Thursday, 5 November 2026")
    assert text.count("/reject/") >= 2
    assert "calendar.google.com" in text
    assert ada["email"] == inbox.messages[0]["To"]
    sent = len(inbox.messages)
    db = client.app.state.session_factory()
    try:
        again = send_weekly_schedules(db, client.app.state.settings, datetime(2026, 11, 4, 10, 0, tzinfo=UTC), host_id=_host_id(db))
        for assignment in db.query(Assignment).all():
            send_assignment_email(db, assignment, client.app.state.settings, datetime.now(UTC))
        db.commit()
    finally:
        db.close()
    assert again[0].status == "sent"
    assert len(inbox.messages) == sent


def test_rejection_get_does_not_change_state_and_post_replans(client, monkeypatch):
    inbox, headers, prop, ada, boa, task = _one_job(client, monkeypatch)
    task = next(item for item in list_tasks(client, headers, prop["id"]) if item["turnover_key"] == "stay-1->stay-2")
    original_id = task["assignment"]["id"]
    original_cleaner = task["assignment"]["cleaner_id"]
    other = boa if original_cleaner == ada["id"] else ada
    token = _task_token(client, task["id"])
    preview = client.get(f"/reject/{token}")
    assert preview.status_code == 200
    assert "Reject this job" in preview.text
    assert "Harbor" in preview.text
    assert list_tasks(client, headers, prop["id"])[0]["assignment"]["id"] == original_id
    declined = client.post(f"/reject/{token}")
    assert declined.status_code == 200
    assert "declined" in declined.text.lower()
    updated = next(item for item in list_tasks(client, headers, prop["id"]) if item["id"] == task["id"])
    assert updated["assignment"]["id"] != original_id
    assert updated["assignment"]["cleaner_id"] == other["id"]
    assert updated["assignment"]["cleaner_id"] != original_cleaner
    assert updated["rejection_cleaner_name"]
    assert updated["replacement_found"] is True
    assert updated["status"] == "confirmed"
    again = client.post(f"/reject/{token}")
    assert again.status_code == 200
    assert "already recorded" in again.text.lower()
    rerun = next(item for item in list_tasks(client, headers, prop["id"]) if item["id"] == task["id"])
    assert rerun["assignment"]["id"] == updated["assignment"]["id"]
    subjects = _subjects(inbox)
    assert any(subject.startswith("Cleaner declined") for subject in subjects)
    assert any(subject.startswith("Cleaning Assignment") for subject in subjects[1:])


def test_unrelated_assignment_stays_and_no_replacement_escalates(client, monkeypatch):
    inbox = Inbox().install(monkeypatch)
    _smtp(client)
    headers = login(client)
    first = create_property(client, headers, name="Harbor", address="12 Bole Road", timezone="Africa/Addis_Ababa")
    second = create_property(client, headers, name="Garden", address="4 Garden Lane", timezone="Africa/Addis_Ababa")
    ada = create_cleaner(
        client,
        headers,
        "Ada",
        timezone="Africa/Addis_Ababa",
        work_start="00:00:00",
        work_end="23:00:00",
        preferred_property_ids=[first["id"], second["id"]],
    )
    sync(client, headers, first["id"], STAYS)
    sync(client, headers, second["id"], [
        {"external_uid": "g1", "start": "2026-11-10T12:00:00Z", "end": "2026-11-12T07:00:00Z"},
        {"external_uid": "g2", "start": "2026-11-12T12:00:00Z", "end": "2026-11-14T07:00:00Z"},
    ])
    harbor = next(item for item in list_tasks(client, headers, first["id"]) if item["assignment"])
    garden = next(item for item in list_tasks(client, headers, second["id"]) if item["assignment"])
    harbor_token = _task_token(client, harbor["id"])
    task_count = _task_count(client)
    client.post(f"/reject/{harbor_token}")
    harbor_after = next(item for item in list_tasks(client, headers, first["id"]) if item["id"] == harbor["id"])
    garden_after = next(item for item in list_tasks(client, headers, second["id"]) if item["id"] == garden["id"])
    assert garden_after["assignment"]["id"] == garden["assignment"]["id"]
    assert harbor_after["assignment"] is None or harbor_after["status"] in {"at_risk", "unassigned"}
    assert harbor_after["replacement_found"] is False
    assert harbor_after["assigned_cleaner_id"] is None
    assert _task_count(client) == task_count
    manager = _plain(next(message for message in reversed(inbox.messages) if message["Subject"].startswith("Cleaner declined")))
    assert "Harbor" in manager
    assert "12 Bole Road" in manager
    assert "No replacement" in manager
    assert ada["id"] == harbor["assignment"]["cleaner_id"]


def test_invalid_expired_and_replaced_links_do_not_change_the_new_assignment(client, monkeypatch):
    inbox, headers, prop, ada, boa, task = _one_job(client, monkeypatch)
    task = next(item for item in list_tasks(client, headers, prop["id"]) if item["turnover_key"] == "stay-1->stay-2")
    missing = client.get("/reject/not-a-real-token")
    assert missing.status_code == 404
    assert client.post("/reject/not-a-real-token").status_code == 404
    token = _task_token(client, task["id"])
    from turnoverops.core.security import hash_token

    db = client.app.state.session_factory()
    try:
        row = db.query(RejectionToken).filter(RejectionToken.token_hash == hash_token(token)).one()
        row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        db.commit()
    finally:
        db.close()
    expired = client.post(f"/reject/{token}")
    assert expired.status_code == 200
    assert "expired" in expired.text.lower()
    assert list_tasks(client, headers, prop["id"])[0]["assignment"]["id"] == task["assignment"]["id"]

    db = client.app.state.session_factory()
    try:
        row = db.query(RejectionToken).filter(RejectionToken.token_hash == hash_token(token)).one()
        row.expires_at = datetime.now(UTC) + timedelta(days=2)
        row.revoked_at = None
        db.commit()
    finally:
        db.close()
    other = boa["id"] if task["assignment"]["cleaner_id"] == ada["id"] else ada["id"]
    moved = client.post(f"/tasks/{task['id']}/reassign", json={"cleaner_id": other}, headers=headers)
    assert moved.status_code == 200, moved.text
    new_id = moved.json()["assignment"]["id"]
    stale = client.post(f"/reject/{token}")
    assert stale.status_code == 200
    assert "replaced" in stale.text.lower()
    current = next(item for item in list_tasks(client, headers, prop["id"]) if item["id"] == task["id"])
    assert current["assignment"]["id"] == new_id
    assert current["assignment"]["cleaner_id"] == other


def test_completed_job_and_scheduler_retry_do_not_duplicate(client, monkeypatch):
    inbox, headers, prop, _ada, _boa, task = _one_job(client, monkeypatch)
    task = next(item for item in list_tasks(client, headers, prop["id"]) if item["turnover_key"] == "stay-1->stay-2")
    token = _task_token(client, task["id"])
    sent = len(inbox.messages)
    assignments = _assignment_count(client)
    client.post("/scheduling/run", json={}, headers=headers)
    assert _assignment_count(client) == assignments
    assert len(inbox.messages) == sent
    db = client.app.state.session_factory()
    try:
        row = db.get(CleaningTask, task["id"])
        row.status = "completed"
        db.commit()
    finally:
        db.close()
    blocked = client.post(f"/reject/{token}")
    assert blocked.status_code == 200
    assert "no longer" in blocked.text.lower()
    db = client.app.state.session_factory()
    try:
        assert db.get(CleaningTask, task["id"]).status == "completed"
        assert db.get(Assignment, task["assignment"]["id"]).state == "accepted"
    finally:
        db.close()


def test_cleaner_email_is_required_and_returned(client):
    headers = login(client)
    rejected = client.post(
        "/cleaners",
        json={"name": "Ada", "email": "not-an-email", "timezone": "UTC", "working_days": [0], "work_start": "09:00:00", "work_end": "17:00:00"},
        headers=headers,
    )
    assert rejected.status_code == 422
    created = create_cleaner(client, headers, "Ada", email="Ada.Mail@Example.com")
    assert created["email"] == "ada.mail@example.com"


def _assignment_count(client) -> int:
    db = client.app.state.session_factory()
    try:
        return db.query(Assignment).count()
    finally:
        db.close()


def _task_count(client) -> int:
    db = client.app.state.session_factory()
    try:
        return db.query(CleaningTask).count()
    finally:
        db.close()


def _host_id(db) -> str:
    return db.query(Host).one().id
