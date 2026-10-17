from datetime import UTC, datetime, timedelta

from turnoverops.integrations.google_calendar_api import CalendarError
from turnoverops.models import Assignment, AuditEvent, CalendarEventMapping, IntegrationState, Notification
from tests.conftest import STAYS, create_cleaner, create_property, list_tasks, login, sync, task_by_key

LATER = [
    {"external_uid": "stay-1", "start": "2026-11-01T15:00:00Z", "end": "2026-11-03T13:00:00Z"},
    {"external_uid": "stay-2", "start": "2026-11-03T17:00:00Z", "end": "2026-11-05T10:00:00Z"},
]


def _setup(client):
    headers = login(client)
    prop = create_property(client, headers)
    ada = create_cleaner(
        client,
        headers,
        "Ada",
        work_start="09:00:00",
        work_end="11:00:00",
        preferred_property_ids=[prop["id"]],
    )
    boa = create_cleaner(
        client,
        headers,
        "Boa",
        work_start="13:00:00",
        work_end="18:00:00",
        preferred_property_ids=[prop["id"]],
    )
    return headers, prop, ada, boa


def _gap(client, headers, property_id):
    return task_by_key(list_tasks(client, headers, property_id), "stay-1->stay-2")


def test_reservation_change_proposes_a_replacement_that_is_not_confirmed(client):
    headers, prop, ada, boa = _setup(client)
    sync(client, headers, prop["id"], STAYS)
    original = _gap(client, headers, prop["id"])
    assert original["status"] == "confirmed"
    assert original["assignment"]["state"] == "accepted"
    assert original["assignment"]["cleaner_id"] == ada["id"]
    sync(client, headers, prop["id"], LATER)
    updated = _gap(client, headers, prop["id"])
    assert updated["status"] == "confirmed"
    assert updated["assignment"]["state"] == "accepted"
    assert updated["assignment"]["cleaner_id"] == boa["id"]
    assert updated["assigned_cleaner_id"] == boa["id"]


def test_accept_confirms_assignment(client):
    headers, prop, ada, _boa = _setup(client)
    sync(client, headers, prop["id"], STAYS)
    task = _gap(client, headers, prop["id"])
    assert task["status"] == "confirmed"
    assert task["assignment"]["state"] == "accepted"
    assert task["assignment"]["cleaner_id"] == ada["id"]
    response = client.post(f"/assignments/{task['assignment']['id']}/accept", headers=headers)
    assert response.status_code == 409


def test_decline_stops_at_the_retry_limit(client):
    headers, prop, _ada, boa = _setup(client)
    cy = create_cleaner(client, headers, "Cy", work_start="13:00:00", work_end="18:00:00")
    dee = create_cleaner(client, headers, "Dee", work_start="13:00:00", work_end="18:00:00")
    sync(client, headers, prop["id"], STAYS)
    sync(client, headers, prop["id"], LATER)
    current = _gap(client, headers, prop["id"])
    assert current["assignment"]["cleaner_id"] == boa["id"]
    declined = client.post(f"/assignments/{current['assignment']['id']}/decline", json={}, headers=headers)
    second = declined.json()
    assert second["status"] == "confirmed"
    assert second["assignment"]["cleaner_id"] in {cy["id"], dee["id"]}
    final = client.post(f"/assignments/{second['assignment']['id']}/decline", json={}, headers=headers).json()
    assert final["status"] == "at_risk"
    assert final["assignment"] is None
    proposed = {cy["id"], dee["id"]} - {second["assignment"]["cleaner_id"]}
    untouched = proposed.pop()
    assert untouched not in (final["assigned_cleaner_id"],)


def test_no_feasible_cleaner_escalates(client):
    headers = login(client)
    prop = create_property(client, headers)
    create_cleaner(client, headers, "Night", work_start="01:00:00", work_end="02:00:00")
    sync(client, headers, prop["id"], STAYS)
    for task in list_tasks(client, headers, prop["id"]):
        assert task["status"] == "at_risk"
        assert task["assigned_cleaner_id"] is None
        assert task["assignment"] is None
    dashboard = client.get("/dashboard", headers=headers).json()
    assert dashboard["metrics"]["at_risk"] >= 1
    assert dashboard["needs_attention"]
    activity = client.get("/activity", params={"event_type": "recovery_exhausted"}, headers=headers).json()
    assert activity["total"] >= 1
    notes = client.get("/notifications", params={"limit": 200}, headers=headers).json()["items"]
    coverage = [item for item in notes if item["message_type"] == "task_unassigned" and item["recipient_type"] == "host"]
    assert coverage
    assert all(item["status"] == "sent" for item in coverage)
    task_id = coverage[0]["task_id"]
    assert "outside_working_hours" not in list_tasks(client, headers, prop["id"])[0]["conflict_details"]["brief"]
    first = client.post("/scheduling/evaluate", json={"task_id": task_id}, headers=headers)
    second = client.post("/scheduling/evaluate", json={"task_id": task_id}, headers=headers)
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["manager_alert"]["manager_notified"] is True
    assert first.json()["manager_alert"]["email_status"] == "not_configured"
    again = [item for item in client.get("/notifications", params={"limit": 200}, headers=headers).json()["items"] if item["message_type"] == "task_unassigned"]
    assert len(again) == len(coverage)


def test_notification_failure_is_retried_without_confirming_the_task(client, monkeypatch):
    import smtplib

    settings = client.app.state.settings
    settings.smtp_host = "smtp.gmail.com"
    settings.smtp_port = 587
    settings.smtp_username = "bot@example.com"
    settings.smtp_password = "app-password"
    settings.email_from = "bot@example.com"
    failed = {"on": True}

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
            return (220, b"ready")

        def login(self, user, password):
            return (235, b"ok")

        def send_message(self, message):
            if failed["on"]:
                raise smtplib.SMTPServerDisconnected("temporary outage")
            return {}

    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    headers, prop, _ada, _boa = _setup(client)
    sync(client, headers, prop["id"], STAYS)
    task = _gap(client, headers, prop["id"])
    assert task["status"] == "confirmed"
    db = client.app.state.session_factory()
    try:
        notes = db.query(Notification).filter(Notification.channel == "smtp", Notification.message_type == "assignment_email").all()
        assert notes
        assert all(note.status == "failed" for note in notes)
        for note in notes:
            note.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
    finally:
        db.close()
    from turnoverops.services.cleaner_mail import retry_smtp_notifications

    db = client.app.state.session_factory()
    try:
        failed["on"] = False
        retry_smtp_notifications(db, client.app.state.settings, now=datetime.now(UTC))
        db.commit()
        notes = db.query(Notification).filter(Notification.channel == "smtp", Notification.message_type == "assignment_email").all()
        assert notes
        assert all(note.status == "sent" for note in notes)
        assert all(note.provider_response["status"] == "sent" for note in notes)
    finally:
        db.close()
    assert _gap(client, headers, prop["id"])["status"] == "confirmed"


def test_calendar_failure_does_not_change_confirmation(client, monkeypatch):
    def fail(*_args, **_kwargs):
        raise CalendarError("calendar down")

    monkeypatch.setattr("turnoverops.integrations.google_calendar_api.upsert_event", fail)
    headers, prop, _ada, _boa = _setup(client)
    db = client.app.state.session_factory()
    try:
        from turnoverops.models import Host

        host = db.query(Host).one()
        db.add(
            IntegrationState(
                host_id=host.id,
                provider="google_calendar",
                key="oauth",
                value={"access_token": "token", "expires_at": datetime.now(UTC).timestamp() + 3600},
            )
        )
        db.commit()
    finally:
        db.close()
    sync(client, headers, prop["id"], STAYS)
    task = _gap(client, headers, prop["id"])
    assert task["status"] == "confirmed"
    db = client.app.state.session_factory()
    try:
        mapping = db.query(CalendarEventMapping).filter(CalendarEventMapping.task_id == task["id"]).one()
        assert mapping.google_event_id is None
        assert mapping.last_error == "calendar down"
        assert db.query(AuditEvent).filter(AuditEvent.event_type == "calendar_sync_failed", AuditEvent.entity_id == task["id"]).count() == 1
        assert db.get(Assignment, task["assignment"]["id"]).state == "accepted"
    finally:
        db.close()


