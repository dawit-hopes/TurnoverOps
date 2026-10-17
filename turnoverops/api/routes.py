import time
from datetime import UTC, datetime

from fastapi import APIRouter, Header, Query
from sqlalchemy import select

from turnoverops.api.deps import AppSettings, CurrentHost, Db
from turnoverops.api.pagination import window
from turnoverops.api.present import cleaner_out, decision_out, tasks_out
from turnoverops.core.errors import AppError
from turnoverops.core.security import create_token, decode_token, secrets_equal
from turnoverops.integrations import google_calendar_api, google_identity
from turnoverops.models import Assignment, AuditEvent, Cleaner, CleaningTask, Notification, Property, Reservation
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
    GoogleAuthIn,
    HostOut,
    IntegrationOut,
    InviteOut,
    NotificationOut,
    OnboardingOut,
    Page,
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
from turnoverops.services.sync import generate_tasks, sync_demo, sync_ical, sync_stored
from turnoverops.services.telegram_flow import handle_update
from turnoverops.services.workflow import (
    accept_assignment,
    close_assignment,
    reassign_task,
    reconcile_open,
    retry_task,
)
from turnoverops.domain import AssignmentState

health_router = APIRouter(tags=["health"])
auth_router = APIRouter(prefix="/auth", tags=["auth"])
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
def patch_task(task_id: str, body: TaskPatch, host: CurrentHost, db: Db) -> TaskOut:
    task = owned(db, CleaningTask, host.id, task_id)
    if body.priority is not None:
        task.priority = body.priority
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
def evaluate(body: TaskIdIn, host: CurrentHost, db: Db) -> DecisionOut:
    task = owned(db, CleaningTask, host.id, body.task_id)
    prop = owned(db, Property, host.id, task.property_id)
    decision = evaluate_window(
        db,
        host.id,
        JobWindow(prop.id, prop.timezone, task.window_start, task.window_end, task.duration_minutes),
        exclude_task_id=task.id,
    )
    return decision_out(decision)


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
def activity(host: CurrentHost, db: Db, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), event_type: str | None = None):
    stmt = select(AuditEvent).where(AuditEvent.host_id == host.id).order_by(AuditEvent.created_at.desc())
    if event_type:
        stmt = stmt.where(AuditEvent.event_type == event_type)
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
        raise AppError(409, "integration_unconfigured", "Google Calendar is not configured")
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
