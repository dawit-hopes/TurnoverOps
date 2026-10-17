import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from turnoverops.integrations.scheduling_agent import AgentCompletion
from turnoverops.integrations.snapshots import FeedError
from turnoverops.models import (
    Assignment,
    CalendarChangeEvent,
    CalendarSyncRun,
    CleaningTask,
    DailyPlanningRun,
    Notification,
    NotificationDelivery,
    Property,
    Reservation,
    ScheduledJobRun,
)
from turnoverops.services.planning.daily import run_due_daily_plans
from turnoverops.services.planning.orchestrate import orchestrate
from turnoverops.services.sync import sync_property_feed
from tests.conftest import create_cleaner, create_property, list_tasks, login

SETTINGS = {
    "timezone": "UTC",
    "default_checkout_time": "10:00:00",
    "default_checkin_time": "15:00:00",
    "default_cleaning_duration_minutes": 60,
    "auto_reassignment": True,
    "trusted_cleaner_policy": False,
    "notify_cleaners": True,
    "notify_host_on_escalation": True,
    "sync_interval_minutes": 5,
    "dispatch_mode": "review_only",
    "daily_plan_time": "00:00:00",
    "daily_planning_enabled": False,
    "allow_fast_path": True,
}


class FakeAgent:
    def __init__(self, responses: list[str] | None = None, behavior: str = "assign_first"):
        self.responses = list(responses or [])
        self.behavior = behavior
        self.calls: list[dict] = []
        self.cleaner_id: str | None = None

    def complete(self, *, system: str, user: str, settings):
        self.calls.append({"system": system, "user": user})
        payload = json.loads(user)
        if payload.get("kind") != "repair" and payload.get("cleaners"):
            self.cleaner_id = payload["cleaners"][0]["id"]
        if self.responses:
            text = self.responses.pop(0)
        elif self.behavior == "overlap_then_fix" and len(self.calls) == 1:
            text = _bad_overlap(payload)
        else:
            text = _assign_first(payload, getattr(self, "cleaner_id", None))
        return AgentCompletion(text=text, prompt_tokens=12, output_tokens=6, model="fake", provider="fake")


def _plan(payload: dict, *, create: list, change: list | None = None, preserve: list | None = None, unassigned: list | None = None) -> str:
    body = {
        "planning_run_id": payload["planning_run_id"],
        "operational_date": payload["operational_date"],
        "timezone": payload["timezone"],
        "assignments_to_create": create,
        "assignments_to_change": change or [],
        "assignments_to_preserve": preserve or [],
        "unassigned_tasks": unassigned or [],
        "conflicts": [],
        "warnings": [],
        "reasoning_summary": "Scheduled the connected turnovers.",
        "requires_human_review": bool(unassigned),
    }
    return json.dumps(body)


def _proposal(task: dict, cleaner_id: str, start: str | None = None, end: str | None = None) -> dict:
    begin = datetime.fromisoformat(start or task["window_start"])
    finish = datetime.fromisoformat(end) if end else begin + timedelta(minutes=task["duration_minutes"])
    return {
        "task_id": task["task_id"],
        "cleaner_id": cleaner_id,
        "start": begin.isoformat(),
        "end": finish.isoformat(),
        "deadline": task["window_end"],
        "explanation": f"Assign {cleaner_id} to this turnover",
    }


def _unassigned(task: dict, reason: str = "no_capacity") -> dict:
    return {
        "task_id": task["task_id"],
        "reason_code": reason,
        "explanation": "No remaining capacity for this turnover",
        "attempted_alternatives": ["The only cleaner is already booked"],
        "next_action": "Review availability or arrange coverage",
    }


def _assign_first(payload: dict, cleaner_id: str | None = None) -> str:
    if payload.get("kind") == "repair":
        tasks = payload["affected_tasks"]
        cleaner_id = cleaner_id or (tasks[0].get("current_assignment") or {}).get("cleaner_id")
        if cleaner_id is None:
            raise AssertionError("repair fixture needs a cleaner id on the affected task")
    else:
        tasks = payload["tasks"]
        cleaner_id = cleaner_id or payload["cleaners"][0]["id"]
    create = [_proposal(tasks[0], cleaner_id)]
    unassigned = [_unassigned(task) for task in tasks[1:]]
    return _plan(payload, create=create, unassigned=unassigned)


def _bad_overlap(payload: dict) -> str:
    tasks = payload["tasks"]
    cleaner_id = payload["cleaners"][0]["id"]
    start = datetime.fromisoformat(tasks[0]["window_end"]) - timedelta(hours=2)
    end = start + timedelta(minutes=tasks[0]["duration_minutes"])
    create = [
        _proposal(task, cleaner_id, start.isoformat(), end.isoformat())
        for task in tasks
    ]
    return _plan(payload, create=create)


def _put_settings(client, headers, **overrides):
    body = dict(SETTINGS)
    body.update(overrides)
    response = client.put("/settings", json=body, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _tomorrow(hour: int) -> datetime:
    return (datetime.now(UTC) + timedelta(days=1)).replace(hour=hour, minute=0, second=0, microsecond=0)


def _stays(checkout_hour: int = 10, gap_hours: int = 5, suffix: str = "", follow: bool = False) -> list[dict]:
    checkout = _tomorrow(checkout_hour)
    rows = [
        {
            "external_uid": f"stay-a{suffix}",
            "start": (checkout - timedelta(days=2)).isoformat(),
            "end": checkout.isoformat(),
        }
    ]
    if follow:
        checkin = checkout + timedelta(hours=gap_hours)
        rows.append(
            {
                "external_uid": f"stay-b{suffix}",
                "start": checkin.isoformat(),
                "end": (checkin + timedelta(days=3)).isoformat(),
            }
        )
    return rows


def _property(client, headers, name: str, stays: list[dict], **overrides) -> dict:
    body = {
        "name": name,
        "timezone": "UTC",
        "cleaning_duration_minutes": 60,
        "feed_kind": "demo",
        "demo_payload": {"reservations": stays},
    }
    body.update(overrides)
    return create_property(client, headers, **body)


def _host_id(client, headers) -> str:
    response = client.get("/auth/me", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _use_agent(app, agent: FakeAgent) -> None:
    app.state.scheduling_client = agent


def _tasks(client, headers) -> list[dict]:
    return list_tasks(client, headers, "") if False else client.get("/tasks", params={"limit": 100}, headers=headers).json()["items"]


def _assignments(app, host_id: str) -> list[Assignment]:
    db = app.state.session_factory()
    try:
        return list(db.scalars(select(Assignment).where(Assignment.host_id == host_id)).all())
    finally:
        db.close()


def _notes(app, host_id: str, recipient: str | None = None) -> list[Notification]:
    db = app.state.session_factory()
    try:
        stmt = select(Notification).where(Notification.host_id == host_id)
        if recipient:
            stmt = stmt.where(Notification.recipient_type == recipient)
        return list(db.scalars(stmt).all())
    finally:
        db.close()


def _deliveries(app, host_id: str) -> list[NotificationDelivery]:
    db = app.state.session_factory()
    try:
        return list(db.scalars(select(NotificationDelivery).where(NotificationDelivery.host_id == host_id)).all())
    finally:
        db.close()


def test_manual_run_calls_the_agent_when_two_tasks_compete_and_get_matches_the_saved_run(client, app):
    headers = login(client)
    _put_settings(client, headers)
    stays = _stays()
    _property(client, headers, "Harbor", stays)
    _property(client, headers, "Annex", _stays(suffix="-2"))
    create_cleaner(client, headers, "Ada", timezone="UTC", work_start="08:00:00", work_end="18:00:00", max_jobs_per_day=1)
    agent = FakeAgent()
    _use_agent(app, agent)
    created = client.post("/planning/runs", headers=headers)
    assert created.status_code == 200, created.text
    body = created.json()
    assert agent.calls
    assert json.loads(agent.calls[0]["user"])["kind"] == "plan"
    saved = client.get(f"/planning/runs/{body['id']}", headers=headers)
    assert saved.status_code == 200, saved.text
    assert saved.json() == body
    assert body["llm_calls"] == 1
    assert body["assignments_created"] == 1
    assert body["unassigned_count"] == 1
    assert body["result"]["assignments_to_create"]
    assert body["result"]["unassigned_tasks"]


def test_two_daily_runs_create_one_task_and_one_assignment(client, app):
    headers = login(client)
    _put_settings(client, headers, daily_planning_enabled=True)
    _property(client, headers, "Harbor", _stays())
    create_cleaner(client, headers, "Ada", timezone="UTC", work_start="08:00:00", work_end="18:00:00")
    confirmed = client.post("/onboarding/confirm-configuration", headers=headers)
    assert confirmed.status_code == 200, confirmed.text
    approved = client.post("/onboarding/approve-plan", json={"dispatch_mode": "review_only"}, headers=headers)
    assert approved.status_code == 200, approved.text
    db = app.state.session_factory()
    try:
        run_due_daily_plans(db, app.state.settings, datetime.now(UTC), client=FakeAgent())
        run_due_daily_plans(db, app.state.settings, datetime.now(UTC), client=FakeAgent())
        db.commit()
        host_id = _host_id(client, headers)
        tasks = db.scalars(select(CleaningTask).where(CleaningTask.host_id == host_id)).all()
        assignments = db.scalars(select(Assignment).where(Assignment.host_id == host_id)).all()
        jobs = db.scalars(select(ScheduledJobRun).where(ScheduledJobRun.host_id == host_id)).all()
    finally:
        db.close()
    assert len(tasks) == 1
    assert len([row for row in assignments if row.state == "accepted"]) == 1
    assert [row.status for row in jobs].count("completed") == 1
    assert [row.status for row in jobs].count("skipped") == 1


def test_feasible_confirmed_assignment_is_preserved(client, app):
    headers = login(client)
    _put_settings(client, headers)
    prop = _property(client, headers, "Harbor", _stays())
    create_cleaner(client, headers, "Ada", timezone="UTC", work_start="08:00:00", work_end="18:00:00")
    from tests.conftest import sync

    sync(client, headers, prop["id"], _stays())
    host_id = _host_id(client, headers)
    before = _assignments(app, host_id)
    assert len(before) == 1
    agent = FakeAgent()
    _use_agent(app, agent)
    run = client.post("/planning/runs", headers=headers)
    assert run.status_code == 200, run.text
    body = run.json()
    assert body["llm_calls"] == 0
    assert body["assignments_created"] == 0
    assert body["assignments_changed"] == 0
    preserved = {item["task_id"] for item in body["result"]["assignments_to_preserve"]}
    assert before[0].task_id in preserved
    assert len(_assignments(app, host_id)) == 1
    assert agent.calls == []


def test_earlier_checkout_changes_the_assignment_only_when_the_old_slot_no_longer_fits(client, app):
    headers = login(client)
    _put_settings(client, headers)
    stays = _stays(checkout_hour=12, gap_hours=6)
    prop = _property(client, headers, "Harbor", stays, default_checkin_time="20:00:00")
    create_cleaner(client, headers, "Ada", timezone="UTC", work_start="08:00:00", work_end="18:00:00")
    _use_agent(app, FakeAgent())
    first = client.post("/planning/runs", headers=headers)
    assert first.status_code == 200, first.text
    host_id = _host_id(client, headers)
    original = _assignments(app, host_id)[0]
    earlier_but_fit = _stays(checkout_hour=10, gap_hours=6)
    _replace_feed(app, prop["id"], earlier_but_fit)
    kept = _replan_change(app, host_id)
    assert kept.assignments_changed == 0
    assert kept.llm_calls == 0
    still = _assignments(app, host_id)
    assert len([row for row in still if row.state == "accepted"]) == 1
    assert still[0].id == original.id
    too_late = _stays(checkout_hour=17, gap_hours=4)
    _replace_feed(app, prop["id"], too_late)
    moved = _replan_change(app, host_id)
    assert moved.assignments_changed == 1 or moved.unassigned_count == 1
    current = [row for row in _assignments(app, host_id) if row.state == "accepted"]
    if current:
        assert current[0].scheduled_start >= _tomorrow(17)


def test_cancelling_a_reservation_keeps_completed_work_and_cancels_unstarted_work_once(client, app):
    headers = login(client)
    _put_settings(client, headers)
    first = _property(client, headers, "Harbor", _stays())
    second = _property(client, headers, "Annex", _stays(suffix="-2"))
    create_cleaner(client, headers, "Ada", timezone="UTC", work_start="08:00:00", work_end="18:00:00", max_jobs_per_day=2)
    _use_agent(app, FakeAgent())
    planned = client.post("/planning/runs", headers=headers)
    assert planned.status_code == 200, planned.text
    host_id = _host_id(client, headers)
    db = app.state.session_factory()
    try:
        done = db.scalar(select(CleaningTask).where(CleaningTask.property_id == first["id"]))
        done.status = "completed"
        open_task = db.scalar(select(CleaningTask).where(CleaningTask.property_id == second["id"]))
        open_id = open_task.id
        db.commit()
        harbor = db.get(Property, first["id"])
        annex = db.get(Property, second["id"])
        harbor.demo_payload = {"reservations": []}
        annex.demo_payload = {"reservations": []}
        now = datetime.now(UTC)
        sync_property_feed(db, harbor, app.state.settings, now)
        from turnoverops.services.sync import rebuild_tasks

        rebuild_tasks(db, harbor, now)
        sync_property_feed(db, annex, app.state.settings, now)
        rebuild_tasks(db, annex, now)
        sync_property_feed(db, annex, app.state.settings, now)
        rebuild_tasks(db, annex, now)
        db.commit()
        completed = db.get(CleaningTask, done.id)
        cancelled = db.get(CleaningTask, open_id)
        removed = db.scalar(select(func.count()).select_from(CleaningTask).where(CleaningTask.id == done.id))
    finally:
        db.close()
    assert removed == 1
    assert completed.status == "completed"
    assert cancelled.status == "blocked"


def test_checkin_change_replans_the_affected_task(client, app):
    headers = login(client)
    _put_settings(client, headers)
    prop = _property(client, headers, "Harbor", _stays(gap_hours=6, follow=True))
    create_cleaner(client, headers, "Ada", timezone="UTC", work_start="08:00:00", work_end="18:00:00")
    _use_agent(app, FakeAgent())
    client.post("/planning/runs", headers=headers)
    checkout = _tomorrow(10)
    checkin = checkout + timedelta(hours=6)
    tight = checkout + timedelta(minutes=30)
    _replace_feed(
        app,
        prop["id"],
        [
            {"external_uid": "stay-a", "start": (checkout - timedelta(days=2)).isoformat(), "end": checkout.isoformat()},
            {"external_uid": "stay-b", "start": tight.isoformat(), "end": (checkin + timedelta(days=3)).isoformat()},
        ],
    )
    host_id = _host_id(client, headers)
    run = _replan_change(app, host_id)
    db = app.state.session_factory()
    try:
        event = db.scalar(select(CalendarChangeEvent).where(CalendarChangeEvent.change_type == "checkin_changed"))
        task = db.scalar(select(CleaningTask).where(CleaningTask.host_id == host_id, CleaningTask.status != "completed"))
    finally:
        db.close()
    assert event is not None
    assert run.trigger == "calendar_change"
    assert run.tasks_considered >= 1
    assert run.unassigned_count == 1 or run.assignments_changed == 1
    assert task.status in {"unassigned", "confirmed", "at_risk"}


def test_two_tasks_one_cleaner_use_a_single_agent_call(client, app):
    headers = login(client)
    _put_settings(client, headers, allow_fast_path=True)
    _property(client, headers, "Harbor", _stays())
    _property(client, headers, "Annex", _stays(suffix="-2"))
    create_cleaner(client, headers, "Ada", timezone="UTC", work_start="08:00:00", work_end="18:00:00", max_jobs_per_day=1)
    agent = FakeAgent()
    _use_agent(app, agent)
    run = client.post("/planning/runs", headers=headers)
    assert run.status_code == 200, run.text
    assert len(agent.calls) == 1
    assert run.json()["assignments_created"] == 1
    assert run.json()["unassigned_count"] == 1
    payload = json.loads(agent.calls[0]["user"])
    assert len(payload["tasks"]) == 2


def test_replaying_a_change_event_does_not_add_assignments_agent_calls_or_emails(client, app):
    headers = login(client)
    _put_settings(client, headers)
    prop = _property(client, headers, "Harbor", _stays(gap_hours=6))
    create_cleaner(client, headers, "Night", timezone="UTC", work_start="01:00:00", work_end="02:00:00")
    agent = FakeAgent()
    _use_agent(app, agent)
    client.post("/planning/runs", headers=headers)
    host_id = _host_id(client, headers)
    deliveries_before = len(_deliveries(app, host_id))
    calls_before = len(agent.calls)
    assignments_before = len(_assignments(app, host_id))
    _replace_feed(app, prop["id"], _stays(checkout_hour=11, gap_hours=5))
    db = app.state.session_factory()
    try:
        row = db.get(Property, prop["id"])
        first = sync_property_feed(db, row, app.state.settings, datetime.now(UTC))
        from turnoverops.services.sync import rebuild_tasks

        rebuild_tasks(db, row, datetime.now(UTC))
        db.commit()
        assert first["events"]
        orchestrate(
            db,
            host_id,
            "calendar_change",
            app.state.settings,
            task_ids=[event.details["task_ids"][0] for event in first["events"] if (event.details or {}).get("task_ids")],
            client=agent,
        )
        second = sync_property_feed(db, row, app.state.settings, datetime.now(UTC))
        db.commit()
    finally:
        db.close()
    assert second["events"] == []
    assert len(agent.calls) == calls_before
    assert len(_deliveries(app, host_id)) == deliveries_before
    assert len(_assignments(app, host_id)) == assignments_before


def test_failed_feed_fetch_leaves_reservations_and_tasks(client, app, monkeypatch):
    headers = login(client)
    _put_settings(client, headers)
    prop = _property(client, headers, "Harbor", _stays())
    create_cleaner(client, headers, "Ada", timezone="UTC", work_start="08:00:00", work_end="18:00:00")
    _use_agent(app, FakeAgent())
    client.post("/planning/runs", headers=headers)
    host_id = _host_id(client, headers)
    before_tasks = len(_tasks(client, headers))
    db = app.state.session_factory()
    try:
        from turnoverops.models import Property

        row = db.get(Property, prop["id"])
        before_reservations = db.scalar(select(func.count()).select_from(Reservation).where(Reservation.property_id == prop["id"]))
        row.feed_kind = "ical"
        row.ical_url = "https://example.com/feed.ics"
        db.commit()
    finally:
        db.close()

    def fail_fetch(*args, **kwargs):
        raise FeedError("calendar unavailable")

    monkeypatch.setattr("turnoverops.services.sync.ical_feed.fetch_feed", fail_fetch)
    run = client.post("/planning/runs", headers=headers)
    assert run.status_code == 200, run.text
    assert run.json()["result"]["sync_failures"]
    db = app.state.session_factory()
    try:
        reservations = db.scalar(select(func.count()).select_from(Reservation).where(Reservation.property_id == prop["id"]))
        tasks = db.scalar(select(func.count()).select_from(CleaningTask).where(CleaningTask.host_id == host_id))
        failed = db.scalar(select(CalendarSyncRun).where(CalendarSyncRun.property_id == prop["id"], CalendarSyncRun.status == "failed"))
    finally:
        db.close()
    assert reservations == before_reservations
    assert tasks == before_tasks
    assert failed is not None


def test_second_trigger_during_a_running_plan_returns_the_active_run(client, app):
    headers = login(client)
    _put_settings(client, headers)
    _property(client, headers, "Harbor", _stays())
    create_cleaner(client, headers, "Ada", timezone="UTC", work_start="08:00:00", work_end="18:00:00")
    host_id = _host_id(client, headers)
    db = app.state.session_factory()
    try:
        active = DailyPlanningRun(
            host_id=host_id,
            trigger="manual",
            status="running",
            stage="coordinating_schedule",
            operational_date=datetime.now(UTC).date().isoformat(),
            timezone="UTC",
            started_at=datetime.now(UTC),
            tasks_considered=0,
            assignments_created=0,
            assignments_preserved=0,
            assignments_changed=0,
            unassigned_count=0,
            llm_calls=0,
            repair_count=0,
            requires_human_review=False,
        )
        db.add(active)
        db.commit()
        active_id = active.id
    finally:
        db.close()
    response = client.post("/planning/runs", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["id"] == active_id
    assert response.json()["status"] == "running"
    assert _assignments(app, host_id) == []
    db = app.state.session_factory()
    try:
        running = db.scalar(select(func.count()).select_from(DailyPlanningRun).where(DailyPlanningRun.status == "running"))
    finally:
        db.close()
    assert running == 1


def test_review_only_persists_the_plan_and_confirmed_work_is_not_renotified(client, app):
    headers = login(client)
    _put_settings(client, headers, notify_cleaners=True, dispatch_mode="review_only")
    prop = _property(client, headers, "Harbor", _stays())
    create_cleaner(client, headers, "Ada", timezone="UTC", work_start="08:00:00", work_end="18:00:00")
    from tests.conftest import sync

    sync(client, headers, prop["id"], _stays())
    host_id = _host_id(client, headers)
    before = len(_notes(app, host_id, "cleaner"))
    _use_agent(app, FakeAgent())
    run = client.post("/planning/runs", headers=headers)
    assert run.status_code == 200, run.text
    assert run.json()["assignments_created"] == 0
    assert len(_notes(app, host_id, "cleaner")) == before
    assert all(row.dispatched_at is None for row in _assignments(app, host_id))


def _replace_feed(app, property_id: str, stays: list[dict]) -> None:
    from turnoverops.models import Property

    db = app.state.session_factory()
    try:
        row = db.get(Property, property_id)
        row.demo_payload = {"reservations": stays}
        db.commit()
    finally:
        db.close()


def _replan_change(app, host_id: str) -> DailyPlanningRun:
    from turnoverops.models import Property
    from turnoverops.services.sync import rebuild_tasks

    db = app.state.session_factory()
    try:
        properties = db.scalars(select(Property).where(Property.host_id == host_id, Property.feed_kind == "demo")).all()
        task_ids: list[str] = []
        now = datetime.now(UTC)
        for prop in properties:
            outcome = sync_property_feed(db, prop, app.state.settings, now)
            if not outcome["events"]:
                continue
            affected = rebuild_tasks(db, prop, now)
            from turnoverops.services.planning.changes import link_event_tasks

            task_ids.extend(link_event_tasks(db, outcome["events"]))
            task_ids.extend(task.id for task in affected)
        db.commit()
        run = orchestrate(
            db,
            host_id,
            "calendar_change",
            app.state.settings,
            task_ids=list(dict.fromkeys(task_ids)),
            client=getattr(app.state, "scheduling_client", None),
            now=now,
        )
        db.commit()
        return run
    finally:
        db.close()
