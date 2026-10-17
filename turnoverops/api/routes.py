import time
from datetime import UTC, datetime

from fastapi import APIRouter, Header, Query, Request
from sqlalchemy import select

from turnoverops.api.deps import AppSettings, CurrentHost, Db
from turnoverops.api.pagination import window
from turnoverops.api.present import cleaner_out, decision_out, tasks_out
from turnoverops.core.errors import AppError
from turnoverops.core.security import create_token, decode_token, secrets_equal
from turnoverops.integrations import google_calendar_api, google_identity
from turnoverops.domain import LOCKED_TASK_STATUSES, AssignmentState, TaskStatus
from turnoverops.models import Assignment, AuditEvent, Cleaner, CleaningTask, Host, Notification, Property, Reservation
from turnoverops.scheduling.engine import JobWindow
from turnoverops.schemas import (
    AuditOut,
    AuthOut,
    CalendarCallbackIn,
    CalendarConnectOut,
    CleanerIn,
    CleanerOut,
    CleanerUpdate,
    DashboardOut,
    DecisionOut,
    ManagerAlertOut,
    BookingShiftIn,
    GoogleAuthIn,
    HostOut,
    SettingsOut,
    SettingsUpdate,
    ApprovePlanIn,
    IntegrationOut,
    InviteOut,
    NotificationOut,
    OnboardingOut,
    Page,
    PlanningCurrentOut,
    PlanningRunOut,
    PreviewIn,
    PropertyIn,
    PropertyOut,
    PropertyUpdate,
    ProposeIn,
    ReasonIn,
    ReservationOut,
    PropertyIdIn,
    RunIn,
    SyncIn,
    SyncOut,
    TaskIdIn,
    TaskOut,
    TaskPatch,
)
from turnoverops.services.auth import upsert_host
from turnoverops.services.calendar_sync import calendar_configured, disconnect, save_oauth
from turnoverops.services.catalog import complete_onboarding, create_cleaner, create_property, onboarding_status, update_cleaner, update_property
from turnoverops.services.dashboard import integration_status, snapshot
from turnoverops.services.evaluation import evaluate_window
from turnoverops.services.invites import create_invite, revoke_invites
from turnoverops.services.scope import owned
from turnoverops.services.host_policy import ensure_settings, load_policy
from turnoverops.services.planning.alerts import alert_unassigned, manager_alert_state
from turnoverops.services.sync import generate_tasks, shift_demo_booking, sync_demo, sync_ical, sync_stored
from turnoverops.services.telegram_flow import handle_update
from turnoverops.services.workflow import (
    accept_assignment,
    close_assignment,
    confirm_waiting_assignments,
    mark_cleaner_unavailable,
    reassign_task,
    reconcile_open,
    reconcile_task,
    resolve_issue,
    retry_task,
)
health_router = APIRouter(tags=["health"])
auth_router = APIRouter(prefix="/auth", tags=["auth"])
settings_router = APIRouter(prefix="/settings", tags=["settings"])
onboarding_router = APIRouter(prefix="/onboarding", tags=["onboarding"])
property_router = APIRouter(prefix="/properties", tags=["properties"])
cleaner_router = APIRouter(prefix="/cleaners", tags=["cleaners"])
reservation_router = APIRouter(prefix="/reservations", tags=["reservations"])
task_router = APIRouter(prefix="/tasks", tags=["tasks"])
scheduling_router = APIRouter(prefix="/scheduling", tags=["scheduling"])
assignment_router = APIRouter(prefix="/assignments", tags=["assignments"])
dashboard_router = APIRouter(prefix="/dashboard", tags=["dashboard"])
activity_router = APIRouter(prefix="/activity", tags=["activity"])
notification_router = APIRouter(prefix="/notifications", tags=["notifications"])
integration_router = APIRouter(prefix="/integrations", tags=["integrations"])
dev_router = APIRouter(prefix="/dev", tags=["dev"])
planning_router = APIRouter(prefix="/planning", tags=["planning"])


@health_router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@auth_router.post("/google", response_model=AuthOut)
def google_login(body: GoogleAuthIn, settings: AppSettings, db: Db) -> AuthOut:
    identity = google_identity.verify_google_id_token(body.id_token, settings.google_client_id)
    host = upsert_host(db, identity)
    token = create_token(host.id, settings, purpose="access", ttl_minutes=settings.jwt_ttl_minutes)
    return AuthOut(access_token=token, host=HostOut.model_validate(host))


@auth_router.get("/me", response_model=HostOut)
def me(host: CurrentHost) -> Host:
    return host


@settings_router.get("", response_model=SettingsOut)
def get_settings(host: CurrentHost, db: Db) -> SettingsOut:
    return SettingsOut.model_validate(ensure_settings(db, host.id))


@settings_router.put("", response_model=SettingsOut)
def put_settings(body: SettingsUpdate, host: CurrentHost, db: Db) -> SettingsOut:
    row = ensure_settings(db, host.id)
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(row, key, value)
    return SettingsOut.model_validate(row)


@onboarding_router.get("", response_model=OnboardingOut)
def get_onboarding(host: CurrentHost, db: Db) -> dict:
    return onboarding_status(db, host)


@onboarding_router.post("/complete", response_model=OnboardingOut)
def finish_onboarding(host: CurrentHost, db: Db) -> dict:
    return complete_onboarding(db, host)


@property_router.get("", response_model=Page[PropertyOut])
def list_properties(host: CurrentHost, db: Db, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), active: bool | None = None):
    stmt = select(Property).where(Property.host_id == host.id).order_by(Property.created_at.desc())
    if active is not None:
        stmt = stmt.where(Property.active.is_(active))
    total, rows = window(db, stmt, limit, offset)
    return Page(items=[PropertyOut.model_validate(row) for row in rows], total=total, limit=limit, offset=offset)


@property_router.post("", response_model=PropertyOut, status_code=201)
def add_property(body: PropertyIn, host: CurrentHost, db: Db) -> Property:
    return create_property(db, host, body)


@property_router.get("/{property_id}", response_model=PropertyOut)
def get_property(property_id: str, host: CurrentHost, db: Db) -> Property:
    return owned(db, Property, host.id, property_id)


@property_router.patch("/{property_id}", response_model=PropertyOut)
def patch_property(property_id: str, body: PropertyUpdate, host: CurrentHost, db: Db) -> Property:
    prop = owned(db, Property, host.id, property_id)
    return update_property(db, prop, body)


@property_router.post("/{property_id}/deactivate", response_model=PropertyOut)
def deactivate_property(property_id: str, host: CurrentHost, db: Db) -> Property:
    prop = owned(db, Property, host.id, property_id)
    prop.active = False
    return prop


@property_router.post("/{property_id}/sync", response_model=SyncOut)
def sync_property(property_id: str, body: SyncIn, host: CurrentHost, settings: AppSettings, db: Db) -> SyncOut:
    prop = owned(db, Property, host.id, property_id)
    now = datetime.now(UTC)
    if body.reservations is not None:
        payload = {"reservations": [item.model_dump(mode="json") for item in body.reservations]}
        result = sync_demo(db, prop, payload, now, settings)
    elif prop.feed_kind == "ical":
        result = sync_ical(db, prop, settings, now)
    else:
        result = sync_stored(db, prop, now, settings)
    return SyncOut(
        feed_kind=prop.feed_kind,
        sync_status=prop.sync_status,
        last_synced_at=prop.last_synced_at,
        last_sync_error=prop.last_sync_error,
        ok=bool(result.get("ok")),
        error=result.get("error"),
        created=result["created"],
        updated=result["updated"],
        cancelled=result["cancelled"],
        tasks_affected=result["tasks_affected"],
    )


@cleaner_router.get("", response_model=Page[CleanerOut])
def list_cleaners(host: CurrentHost, db: Db, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), active: bool | None = None):
    stmt = select(Cleaner).where(Cleaner.host_id == host.id).order_by(Cleaner.created_at.desc())
    if active is not None:
        stmt = stmt.where(Cleaner.active.is_(active))
    total, rows = window(db, stmt, limit, offset)
    return Page(items=[cleaner_out(db, row) for row in rows], total=total, limit=limit, offset=offset)


@cleaner_router.post("", response_model=CleanerOut, status_code=201)
def add_cleaner(body: CleanerIn, host: CurrentHost, db: Db) -> CleanerOut:
    return cleaner_out(db, create_cleaner(db, host, body))


@cleaner_router.get("/{cleaner_id}", response_model=CleanerOut)
def get_cleaner(cleaner_id: str, host: CurrentHost, db: Db) -> CleanerOut:
    return cleaner_out(db, owned(db, Cleaner, host.id, cleaner_id))


@cleaner_router.patch("/{cleaner_id}", response_model=CleanerOut)
def patch_cleaner(cleaner_id: str, body: CleanerUpdate, host: CurrentHost, db: Db) -> CleanerOut:
    cleaner = owned(db, Cleaner, host.id, cleaner_id)
    return cleaner_out(db, update_cleaner(db, cleaner, body))


@cleaner_router.post("/{cleaner_id}/deactivate", response_model=CleanerOut)
def deactivate_cleaner(cleaner_id: str, host: CurrentHost, db: Db) -> CleanerOut:
    cleaner = owned(db, Cleaner, host.id, cleaner_id)
    cleaner.active = False
    return cleaner_out(db, cleaner)


@cleaner_router.post("/{cleaner_id}/invites", response_model=InviteOut)
def invite_cleaner(cleaner_id: str, host: CurrentHost, settings: AppSettings, db: Db) -> InviteOut:
    cleaner = owned(db, Cleaner, host.id, cleaner_id)
    url, expires_at = create_invite(db, cleaner, settings)
    return InviteOut(invite_url=url, expires_at=expires_at)


@cleaner_router.post("/{cleaner_id}/invites/revoke", status_code=204)
def revoke_cleaner_invites(cleaner_id: str, host: CurrentHost, db: Db) -> None:
    cleaner = owned(db, Cleaner, host.id, cleaner_id)
    revoke_invites(db, cleaner)


@reservation_router.get("", response_model=Page[ReservationOut])
def list_reservations(
    host: CurrentHost,
    db: Db,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    property_id: str | None = None,
    include_cancelled: bool = False,
):
    if property_id:
        owned(db, Property, host.id, property_id)
    stmt = select(Reservation).where(Reservation.host_id == host.id).order_by(Reservation.start_at)
    if property_id:
        stmt = stmt.where(Reservation.property_id == property_id)
    if not include_cancelled:
        stmt = stmt.where(Reservation.cancelled.is_(False))
    total, rows = window(db, stmt, limit, offset)
    return Page(items=[ReservationOut.model_validate(row) for row in rows], total=total, limit=limit, offset=offset)


@reservation_router.get("/{reservation_id}", response_model=ReservationOut)
def get_reservation(reservation_id: str, host: CurrentHost, db: Db) -> Reservation:
    return owned(db, Reservation, host.id, reservation_id)


@task_router.get("", response_model=Page[TaskOut])
def list_tasks(
    host: CurrentHost,
    db: Db,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    property_id: str | None = None,
    status: str | None = None,
):
    if property_id:
        owned(db, Property, host.id, property_id)
    confirm_waiting_assignments(db, datetime.now(UTC), host.id)
    stmt = select(CleaningTask).where(CleaningTask.host_id == host.id).order_by(CleaningTask.window_start)
    if property_id:
        stmt = stmt.where(CleaningTask.property_id == property_id)
    if status:
        stmt = stmt.where(CleaningTask.status == status)
    total, rows = window(db, stmt, limit, offset)
    return Page(items=tasks_out(db, list(rows)), total=total, limit=limit, offset=offset)


@task_router.post("/generate", response_model=list[TaskOut])
def generate(body: PropertyIdIn, host: CurrentHost, settings: AppSettings, db: Db):
    prop = owned(db, Property, host.id, body.property_id)
    generate_tasks(db, prop, datetime.now(UTC), settings)
    tasks = db.scalars(select(CleaningTask).where(CleaningTask.property_id == prop.id)).all()
    return tasks_out(db, list(tasks))


@task_router.get("/{task_id}", response_model=TaskOut)
def get_task(task_id: str, host: CurrentHost, db: Db) -> TaskOut:
    task = owned(db, CleaningTask, host.id, task_id)
    return tasks_out(db, [task])[0]


@task_router.patch("/{task_id}", response_model=TaskOut)
def patch_task(task_id: str, body: TaskPatch, host: CurrentHost, settings: AppSettings, db: Db) -> TaskOut:
    task = owned(db, CleaningTask, host.id, task_id)
    if body.priority is not None:
        task.priority = body.priority
    if body.window_start is not None and body.window_end is not None and body.duration_minutes is not None:
        if task.started_at is not None or task.status in LOCKED_TASK_STATUSES:
            raise AppError(409, "task_locked", "This cleaning can no longer be edited.")
        task.window_start = body.window_start
        task.window_end = body.window_end
        task.duration_minutes = body.duration_minutes
        task.window_overridden = True
        prop = owned(db, Property, host.id, task.property_id)
        reconcile_task(db, task, prop, datetime.now(UTC), settings, actor="host", policy=load_policy(db, host.id))
    return tasks_out(db, [task])[0]


@task_router.post("/{task_id}/resolve", response_model=TaskOut)
def resolve_task(task_id: str, host: CurrentHost, db: Db) -> TaskOut:
    task = owned(db, CleaningTask, host.id, task_id)
    resolve_issue(db, task, datetime.now(UTC))
    return tasks_out(db, [task])[0]


@task_router.post("/{task_id}/unavailable", response_model=TaskOut)
def unavailable(task_id: str, host: CurrentHost, settings: AppSettings, db: Db) -> TaskOut:
    task = owned(db, CleaningTask, host.id, task_id)
    prop = owned(db, Property, host.id, task.property_id)
    mark_cleaner_unavailable(db, task, prop, datetime.now(UTC), settings)
    return tasks_out(db, [task])[0]


@task_router.post("/{task_id}/booking-shift", response_model=TaskOut)
def booking_shift(task_id: str, body: BookingShiftIn, host: CurrentHost, settings: AppSettings, db: Db) -> TaskOut:
    task = owned(db, CleaningTask, host.id, task_id)
    prop = owned(db, Property, host.id, task.property_id)
    shift_demo_booking(db, task, prop, body.days, datetime.now(UTC), settings)
    db.refresh(task)
    return tasks_out(db, [task])[0]


@task_router.post("/{task_id}/assignments", response_model=TaskOut)
def propose(task_id: str, body: ProposeIn, host: CurrentHost, settings: AppSettings, db: Db) -> TaskOut:
    task = owned(db, CleaningTask, host.id, task_id)
    prop = owned(db, Property, host.id, task.property_id)
    reassign_task(db, task, prop, datetime.now(UTC), settings, cleaner_id=body.cleaner_id)
    return tasks_out(db, [task])[0]


@task_router.post("/{task_id}/reassign", response_model=TaskOut)
def reassign(task_id: str, body: ProposeIn, host: CurrentHost, settings: AppSettings, db: Db) -> TaskOut:
    return propose(task_id, body, host, settings, db)


@task_router.post("/{task_id}/assignment-email/retry", response_model=TaskOut)
def retry_assignment_email(task_id: str, host: CurrentHost, settings: AppSettings, db: Db) -> TaskOut:
    from turnoverops.services.cleaner_mail import retry_task_assignment_email

    task = owned(db, CleaningTask, host.id, task_id)
    retry_task_assignment_email(db, task, settings, datetime.now(UTC))
    return tasks_out(db, [task])[0]


@task_router.post("/{task_id}/retry", response_model=TaskOut)
def retry(task_id: str, host: CurrentHost, settings: AppSettings, db: Db) -> TaskOut:
    task = owned(db, CleaningTask, host.id, task_id)
    prop = owned(db, Property, host.id, task.property_id)
    retry_task(db, task, prop, datetime.now(UTC), settings)
    return tasks_out(db, [task])[0]


@assignment_router.post("/{assignment_id}/accept", response_model=TaskOut)
def accept(assignment_id: str, host: CurrentHost, settings: AppSettings, db: Db) -> TaskOut:
    assignment = owned(db, Assignment, host.id, assignment_id)
    task = accept_assignment(db, assignment, datetime.now(UTC), settings, actor="host")
    return tasks_out(db, [task])[0]


@assignment_router.post("/{assignment_id}/decline", response_model=TaskOut)
def decline(assignment_id: str, host: CurrentHost, settings: AppSettings, db: Db, body: ReasonIn | None = None) -> TaskOut:
    assignment = owned(db, Assignment, host.id, assignment_id)
    task = close_assignment(
        db,
        assignment,
        datetime.now(UTC),
        settings,
        state=AssignmentState.declined,
        actor="host",
        reason=(body.reason if body and body.reason else "Assignment declined"),
    )
    return tasks_out(db, [task])[0]


@scheduling_router.post("/preview", response_model=DecisionOut)
def preview(body: PreviewIn, host: CurrentHost, db: Db) -> DecisionOut:
    prop = owned(db, Property, host.id, body.property_id)
    decision = evaluate_window(
        db,
        host.id,
        JobWindow(prop.id, prop.timezone, body.window_start, body.window_end, body.duration_minutes),
    )
    return decision_out(decision)


@scheduling_router.post("/evaluate", response_model=DecisionOut)
def evaluate(body: TaskIdIn, host: CurrentHost, settings: AppSettings, db: Db) -> DecisionOut:
    task = owned(db, CleaningTask, host.id, body.task_id)
    prop = owned(db, Property, host.id, task.property_id)
    decision = evaluate_window(
        db,
        host.id,
        JobWindow(prop.id, prop.timezone, task.window_start, task.window_end, task.duration_minutes),
        exclude_task_id=task.id,
    )
    alert = None
    needs_cover = task.status in {TaskStatus.at_risk, TaskStatus.unassigned, TaskStatus.blocked}
    if needs_cover and not decision.ranked_feasible and load_policy(db, host.id).notify_host_on_escalation:
        record = db.get(Host, host.id)
        if record is not None:
            reason = str((task.conflict_details or {}).get("summary") or "No feasible cleaner can take this turnover")
            alert_unassigned(
                db,
                record,
                task,
                prop,
                settings,
                reason=reason,
                cleaner_ids=[item.cleaner_id for item in decision.rejected],
            )
        notified, email_status = manager_alert_state(db, task.id)
        alert = ManagerAlertOut(manager_notified=notified, email_status=email_status)
    return decision_out(decision, alert)


@scheduling_router.post("/run", response_model=list[TaskOut])
def run(body: RunIn, host: CurrentHost, settings: AppSettings, db: Db):
    stmt = select(Property).where(Property.host_id == host.id, Property.active.is_(True))
    if body.property_id:
        owned(db, Property, host.id, body.property_id)
        stmt = stmt.where(Property.id == body.property_id)
    now = datetime.now(UTC)
    tasks: list[CleaningTask] = []
    for prop in db.scalars(stmt).all():
        tasks.extend(reconcile_open(db, prop, now, settings))
    return tasks_out(db, tasks)


@dashboard_router.get("", response_model=DashboardOut)
def dashboard(host: CurrentHost, db: Db) -> DashboardOut:
    data = snapshot(db, host)
    return DashboardOut(
        metrics=data["metrics"],
        today=tasks_out(db, data["today"]),
        upcoming=tasks_out(db, data["upcoming"]),
        needs_attention=tasks_out(db, data["needs_attention"]),
    )


@activity_router.get("", response_model=Page[AuditOut])
def activity(
    host: CurrentHost,
    db: Db,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    event_type: str | None = None,
    entity_id: str | None = None,
):
    stmt = select(AuditEvent).where(AuditEvent.host_id == host.id).order_by(AuditEvent.created_at.desc())
    if event_type:
        stmt = stmt.where(AuditEvent.event_type == event_type)
    if entity_id:
        stmt = stmt.where(AuditEvent.entity_id == entity_id)
    total, rows = window(db, stmt, limit, offset)
    return Page(items=[AuditOut.model_validate(row) for row in rows], total=total, limit=limit, offset=offset)


@notification_router.get("", response_model=Page[NotificationOut])
def notifications(
    host: CurrentHost,
    db: Db,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status: str | None = None,
    channel: str | None = None,
):
    stmt = select(Notification).where(Notification.host_id == host.id).order_by(Notification.created_at.desc())
    if status:
        stmt = stmt.where(Notification.status == status)
    if channel:
        stmt = stmt.where(Notification.channel == channel)
    total, rows = window(db, stmt, limit, offset)
    return Page(items=[NotificationOut.model_validate(row) for row in rows], total=total, limit=limit, offset=offset)


@integration_router.get("", response_model=IntegrationOut)
def integrations(host: CurrentHost, settings: AppSettings, db: Db) -> dict:
    return integration_status(db, host, settings)


@integration_router.post("/google-calendar/connect", response_model=CalendarConnectOut)
def connect_calendar(host: CurrentHost, settings: AppSettings) -> CalendarConnectOut:
    if not calendar_configured(settings):
        raise AppError(409, "integration_unconfigured", "Google Calendar is not set up on the server.")
    state = create_token(host.id, settings, purpose="calendar_oauth", ttl_minutes=10)
    return CalendarConnectOut(authorization_url=google_calendar_api.authorization_url(settings, state))


@integration_router.post("/google-calendar/callback", status_code=204)
def calendar_callback(body: CalendarCallbackIn, host: CurrentHost, settings: AppSettings, db: Db) -> None:
    payload = decode_token(body.state, settings, purpose="calendar_oauth")
    if payload.get("sub") != host.id:
        raise AppError(401, "unauthorized", "Invalid calendar authorization state")
    tokens = google_calendar_api.exchange_code(settings, body.code)
    save_oauth(
        db,
        host.id,
        {
            "access_token": tokens["access_token"],
            "refresh_token": tokens.get("refresh_token"),
            "expires_at": time.time() + int(tokens.get("expires_in") or 3600),
        },
    )


@integration_router.post("/google-calendar/disconnect", status_code=204)
def disconnect_calendar(host: CurrentHost, db: Db) -> None:
    disconnect(db, host.id)


@integration_router.post("/telegram/webhook")
def telegram_webhook(
    update: dict,
    settings: AppSettings,
    db: Db,
    secret: str | None = Header(default=None, alias="X-Telegram-Bot-Api-Secret-Token"),
) -> dict:
    if not settings.telegram_webhook_secret:
        raise AppError(503, "webhook_unconfigured", "Telegram webhook secret is not configured")
    if secret is None or not secrets_equal(secret, settings.telegram_webhook_secret):
        raise AppError(401, "unauthorized", "Invalid webhook secret")
    return handle_update(db, update, settings)


@dev_router.post("/telegram/updates")
def simulate_telegram(update: dict, settings: AppSettings, db: Db) -> dict:
    result = handle_update(db, update, settings)
    result["simulated"] = True
    return result


def _planning_client(request: Request):
    return getattr(request.app.state, "scheduling_client", None)


def _planning_out(run) -> PlanningRunOut:
    return PlanningRunOut(
        id=run.id,
        host_id=run.host_id,
        trigger=run.trigger,
        status=run.status,
        stage=run.stage,
        operational_date=run.operational_date,
        timezone=run.timezone,
        started_at=run.started_at,
        finished_at=run.finished_at,
        error=run.error,
        tasks_considered=run.tasks_considered or 0,
        assignments_created=run.assignments_created or 0,
        assignments_preserved=run.assignments_preserved or 0,
        assignments_changed=run.assignments_changed or 0,
        unassigned_count=run.unassigned_count or 0,
        conflicts=list(run.conflicts or []),
        warnings=list(run.warnings or []),
        reasoning_summary=run.reasoning_summary,
        requires_human_review=bool(run.requires_human_review),
        llm_skipped_reason=run.llm_skipped_reason,
        llm_provider=run.llm_provider,
        llm_model=run.llm_model,
        prompt_tokens=run.prompt_tokens,
        output_tokens=run.output_tokens,
        llm_calls=run.llm_calls or 0,
        repair_count=run.repair_count or 0,
        validation_outcome=run.validation_outcome,
        duration_ms=run.duration_ms,
        estimated_cost=run.estimated_cost,
        scope_fingerprint=run.scope_fingerprint,
        result=run.result,
        details=run.details,
    )


@planning_router.post("/runs", response_model=PlanningRunOut)
def start_planning_run(host: CurrentHost, settings: AppSettings, db: Db, request: Request) -> PlanningRunOut:
    from turnoverops.services.planning.orchestrate import orchestrate

    run = orchestrate(db, host.id, "manual", settings, client=_planning_client(request))
    return _planning_out(run)


@planning_router.get("/runs/current", response_model=PlanningCurrentOut)
def current_planning_run(host: CurrentHost, db: Db) -> PlanningCurrentOut:
    from turnoverops.models import DailyPlanningRun

    running = db.scalar(
        select(DailyPlanningRun)
        .where(DailyPlanningRun.host_id == host.id, DailyPlanningRun.status == "running")
        .order_by(DailyPlanningRun.started_at.desc())
    )
    latest = db.scalar(
        select(DailyPlanningRun)
        .where(DailyPlanningRun.host_id == host.id)
        .order_by(DailyPlanningRun.started_at.desc())
    )
    successful = db.scalar(
        select(DailyPlanningRun)
        .where(
            DailyPlanningRun.host_id == host.id,
            DailyPlanningRun.status.in_(("completed", "partial")),
            DailyPlanningRun.validation_outcome.in_(("passed", "skipped")),
        )
        .order_by(DailyPlanningRun.started_at.desc())
    )
    current = running or latest
    return PlanningCurrentOut(
        run=_planning_out(current) if current is not None else None,
        last_successful=_planning_out(successful) if successful is not None else None,
    )


@planning_router.post("/weekly-emails")
def send_weekly_emails(host: CurrentHost, settings: AppSettings, db: Db) -> dict:
    from turnoverops.services.cleaner_mail import send_weekly_schedules

    results = send_weekly_schedules(db, settings, datetime.now(UTC), host_id=host.id)
    return {
        "results": [
            {"cleaner_id": item.cleaner_id, "status": item.status, "jobs": item.jobs}
            for item in results
        ]
    }


@planning_router.get("/runs/{run_id}", response_model=PlanningRunOut)
def get_planning_run(run_id: str, host: CurrentHost, db: Db) -> PlanningRunOut:
    from turnoverops.models import DailyPlanningRun
    from turnoverops.services.scope import owned

    return _planning_out(owned(db, DailyPlanningRun, host.id, run_id))


@onboarding_router.post("/confirm-configuration", response_model=PlanningRunOut)
def confirm_configuration(host: CurrentHost, settings: AppSettings, db: Db, request: Request) -> PlanningRunOut:
    from datetime import UTC, datetime

    from turnoverops.services.host_policy import ensure_settings
    from turnoverops.services.planning.orchestrate import orchestrate

    row = ensure_settings(db, host.id)
    if row.configuration_confirmed_at is None:
        row.configuration_confirmed_at = datetime.now(UTC)
        db.commit()
    run = orchestrate(db, host.id, "onboarding", settings, client=_planning_client(request), force_review=True)
    return _planning_out(run)


@onboarding_router.post("/approve-plan", response_model=SettingsOut)
def approve_plan(body: ApprovePlanIn, host: CurrentHost, settings: AppSettings, db: Db) -> SettingsOut:
    from datetime import UTC, datetime

    from turnoverops.services.host_policy import ensure_settings
    from turnoverops.services.planning.alerts import dispatch_pending

    row = ensure_settings(db, host.id)
    if row.configuration_confirmed_at is None:
        raise AppError(409, "configuration_unconfirmed", "Confirm the schedule before approving it")
    now = datetime.now(UTC)
    row.plan_approved_at = now
    row.dispatch_mode = body.dispatch_mode
    row.daily_planning_enabled = True
    if body.dispatch_mode == "automatic":
        dispatch_pending(db, host.id, settings, now, notify=row.notify_cleaners)
    return SettingsOut.model_validate(row)
