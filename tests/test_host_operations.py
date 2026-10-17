from datetime import UTC, date, datetime, time

from turnoverops.scheduling.engine import decide
from turnoverops.services.sync import sync_due_icals
from tests.conftest import STAYS, create_cleaner, create_property, list_tasks, login, sync, task_by_key
from tests.test_scheduling import cleaner, window
from tests.test_workflow import LATER, _gap, _setup

SETTINGS = {
    "timezone": "Africa/Addis_Ababa",
    "default_checkout_time": "10:00:00",
    "default_checkin_time": "15:00:00",
    "default_cleaning_duration_minutes": 120,
    "auto_reassignment": True,
    "trusted_cleaner_policy": False,
    "notify_cleaners": True,
    "notify_host_on_escalation": True,
    "sync_interval_minutes": 30,
}


def _put_settings(client, headers, **overrides):
    body = dict(SETTINGS)
    body.update(overrides)
    response = client.put("/settings", json=body, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_settings_are_created_on_first_read_and_fill_omitted_property_defaults(client):
    headers = login(client)
    created = client.get("/settings", headers=headers)
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["auto_reassignment"] is True
    assert body["trusted_cleaner_policy"] is True
    assert body["notify_cleaners"] is True
    assert body["notify_host_on_escalation"] is True
    assert body["sync_interval_minutes"] == 5
    assert body["timezone"] == "Africa/Addis_Ababa"
    _put_settings(client, headers, timezone="Europe/London", default_cleaning_duration_minutes=90)
    prop = client.post("/properties", json={"name": "Bare"}, headers=headers)
    assert prop.status_code == 201, prop.text
    assert prop.json()["timezone"] == "Europe/London"
    assert prop.json()["cleaning_duration_minutes"] == 90
    assert prop.json()["default_checkout_time"].startswith("10:00")


def test_trusted_policy_confirms_immediately_and_auto_off_does_not_pick_the_next_cleaner(client):
    headers = login(client, "trusted-host")
    prop = create_property(client, headers)
    ada = create_cleaner(client, headers, "Ada", work_start="09:00:00", work_end="11:00:00", preferred_property_ids=[prop["id"]])
    create_cleaner(client, headers, "Boa", work_start="13:00:00", work_end="18:00:00", preferred_property_ids=[prop["id"]])
    _put_settings(client, headers, trusted_cleaner_policy=True)
    sync(client, headers, prop["id"], STAYS)
    confirmed = _gap(client, headers, prop["id"])
    assert confirmed["status"] == "confirmed"
    assert confirmed["assignment"]["state"] == "accepted"
    assert confirmed["assignment"]["cleaner_id"] == ada["id"]

    headers = login(client, "manual-host")
    prop = create_property(client, headers)
    ada = create_cleaner(client, headers, "Ada", work_start="09:00:00", work_end="11:00:00", preferred_property_ids=[prop["id"]])
    boa = create_cleaner(client, headers, "Boa", work_start="13:00:00", work_end="18:00:00", preferred_property_ids=[prop["id"]])
    _put_settings(client, headers, auto_reassignment=False, trusted_cleaner_policy=False, notify_cleaners=False)
    sync(client, headers, prop["id"], STAYS)
    proposed = _gap(client, headers, prop["id"])
    assert proposed["status"] == "confirmed"
    assert proposed["assignment"]["state"] == "accepted"
    assert proposed["assignment"]["cleaner_id"] == ada["id"]
    notes = client.get("/notifications", params={"limit": 200}, headers=headers).json()["items"]
    assert all(item["recipient_type"] != "cleaner" or item["task_id"] != proposed["id"] for item in notes)
    declined = client.post(f"/assignments/{proposed['assignment']['id']}/decline", json={}, headers=headers)
    assert declined.status_code == 200, declined.text
    parked = declined.json()
    assert parked["status"] == "at_risk"
    assert parked["assignment"] is None
    assert parked["assigned_cleaner_id"] is None
    assert parked["assigned_cleaner_id"] != boa["id"]
    host_assign = client.post(
        f"/tasks/{parked['id']}/assignments",
        json={"cleaner_id": boa["id"]},
        headers=headers,
    )
    assert host_assign.status_code == 200, host_assign.text
    assert host_assign.json()["assigned_cleaner_id"] == boa["id"]
    assert host_assign.json()["status"] == "confirmed"


def test_unavailable_date_is_rejected_by_the_engine(client):
    decision = decide([cleaner("a", unavailable_dates=frozenset({date(2026, 11, 3)}))], window(), [])
    assert decision.selected is None
    assert "unavailable" in decision.rejected[0].reasons

    headers = login(client)
    prop = create_property(client, headers)
    blocked = create_cleaner(client, headers, "Off", unavailable_dates=["2026-11-03"])
    other = create_cleaner(client, headers, "Open", work_start="09:00:00", work_end="17:00:00")
    sync(client, headers, prop["id"], STAYS)
    task = task_by_key(list_tasks(client, headers, prop["id"]), "stay-1->stay-2")
    assert task["assigned_cleaner_id"] == other["id"]
    assert task["assigned_cleaner_id"] != blocked["id"]
    preview = client.post("/scheduling/evaluate", json={"task_id": task["id"]}, headers=headers)
    assert preview.status_code == 200, preview.text
    rejected = next(item for item in preview.json()["candidates"] if item["cleaner_id"] == blocked["id"])
    assert rejected["feasible"] is False
    assert "unavailable" in rejected["reasons"]


def test_manual_window_survives_an_unchanged_resync_and_yields_when_the_reservation_moves(client):
    headers, prop, _ada, _boa = _setup(client)
    sync(client, headers, prop["id"], STAYS)
    task = _gap(client, headers, prop["id"])
    patched = client.patch(
        f"/tasks/{task['id']}",
        json={
            "window_start": "2026-11-03T11:00:00Z",
            "window_end": "2026-11-03T14:00:00Z",
            "duration_minutes": 60,
        },
        headers=headers,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["window_overridden"] is True
    assert patched.json()["window_start"].startswith("2026-11-03T11:00:00")
    assert patched.json()["feed_window_start"].startswith("2026-11-03T10:00:00")
    sync(client, headers, prop["id"], STAYS)
    kept = _gap(client, headers, prop["id"])
    assert kept["window_overridden"] is True
    assert kept["window_start"].startswith("2026-11-03T11:00:00")
    assert kept["window_end"].startswith("2026-11-03T14:00:00")
    sync(client, headers, prop["id"], LATER)
    moved = _gap(client, headers, prop["id"])
    assert moved["window_overridden"] is False
    assert moved["window_start"].startswith("2026-11-03T13:00:00")
    assert moved["feed_window_start"].startswith("2026-11-03T13:00:00")


def test_guest_name_is_stored_from_demo_sync_and_ical_summary():
    from turnoverops.integrations.demo_feed import parse_demo_feed
    from turnoverops.integrations.ical_feed import parse_ical
    from zoneinfo import ZoneInfo

    snapshots = parse_demo_feed(
        {"reservations": [{**STAYS[0], "summary": "Stay", "guest_name": "Lulit Abebe"}]}
    )
    assert snapshots[0].guest_name == "Lulit Abebe"
    assert snapshots[0].summary == "Stay"
    ical = parse_ical(
        "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:stay-1\nDTSTART:20261101T150000Z\nDTEND:20261103T100000Z\nSUMMARY:Lulit Abebe\nEND:VEVENT\nEND:VCALENDAR\n",
        zone=ZoneInfo("UTC"),
        default_checkin=time(15, 0),
        default_checkout=time(10, 0),
    )
    assert ical[0].summary == "Lulit Abebe"
    assert ical[0].guest_name == "Lulit Abebe"


def test_guest_name_round_trips_on_demo_sync(client):
    headers = login(client)
    prop = create_property(client, headers)
    create_cleaner(client, headers, "Ada")
    stays = [{**STAYS[0], "guest_name": "Lulit Abebe", "summary": "Harbor stay"}, STAYS[1]]
    sync(client, headers, prop["id"], stays)
    rows = client.get("/reservations", params={"property_id": prop["id"]}, headers=headers).json()["items"]
    named = next(row for row in rows if row["external_uid"] == "stay-1")
    assert named["guest_name"] == "Lulit Abebe"
    assert named["summary"] == "Harbor stay"


def test_resolved_task_drops_out_of_dashboard_attention(client):
    headers = login(client)
    prop = create_property(client, headers)
    create_cleaner(client, headers, "Night", work_start="01:00:00", work_end="02:00:00")
    sync(client, headers, prop["id"], STAYS)
    dashboard = client.get("/dashboard", headers=headers).json()
    assert dashboard["needs_attention"]
    task_id = dashboard["needs_attention"][0]["id"]
    resolved = client.post(f"/tasks/{task_id}/resolve", headers=headers)
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["host_resolved_at"] is not None
    again = client.get("/dashboard", headers=headers).json()
    assert task_id not in {item["id"] for item in again["needs_attention"]}
    assert all(item["id"] != task_id for item in again["needs_attention"])


def test_sync_interval_skips_a_property_until_it_elapses(client, app, monkeypatch):
    monkeypatch.setattr("turnoverops.services.catalog.validate_public_http_url", lambda *args, **kwargs: None)
    headers = login(client)
    prop = create_property(client, headers, feed_kind="ical", ical_url="https://example.com/feed.ics")
    _put_settings(client, headers, sync_interval_minutes=30)
    db = app.state.session_factory()
    try:
        from turnoverops.models import Property

        row = db.get(Property, prop["id"])
        row.last_synced_at = datetime.now(UTC)
        row.sync_status = "ok"
        db.commit()
    finally:
        db.close()
    called = {"count": 0}

    def fail_fetch(*args, **kwargs):
        called["count"] += 1
        raise AssertionError("feed should not be fetched inside the interval")

    monkeypatch.setattr("turnoverops.services.sync.ical_feed.fetch_feed", fail_fetch)
    db = app.state.session_factory()
    try:
        sync_due_icals(db, app.state.settings, datetime.now(UTC))
        db.commit()
    finally:
        db.close()
    assert called["count"] == 0


def test_booking_shift_moves_demo_reservations_and_rejects_ical(client, monkeypatch):
    monkeypatch.setattr("turnoverops.services.catalog.validate_public_http_url", lambda *args, **kwargs: None)
    headers, prop, _ada, _boa = _setup(client)
    sync(client, headers, prop["id"], STAYS)
    task = _gap(client, headers, prop["id"])
    shifted = client.post(f"/tasks/{task['id']}/booking-shift", json={"days": 1}, headers=headers)
    assert shifted.status_code == 200, shifted.text
    assert shifted.json()["window_start"].startswith("2026-11-04T10:00:00")
    updated = client.patch(
        f"/properties/{prop['id']}",
        json={"feed_kind": "ical", "ical_url": "https://example.com/feed.ics"},
        headers=headers,
    )
    assert updated.status_code == 200, updated.text
    denied = client.post(f"/tasks/{task['id']}/booking-shift", json={"days": 1}, headers=headers)
    assert denied.status_code == 409
    assert denied.json()["error"]["code"] == "feed_owned"
