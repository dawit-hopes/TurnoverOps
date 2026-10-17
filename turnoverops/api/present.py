from sqlalchemy import select
from sqlalchemy.orm import Session

from turnoverops.domain import LIVE_ASSIGNMENT_STATES, AssignmentState
from turnoverops.models import Assignment, Cleaner, CleanerProperty, CleaningTask, Notification, NotificationDelivery
from turnoverops.scheduling.engine import Decision, describe
from turnoverops.schemas import AssignmentOut, CandidateOut, CleanerOut, DecisionOut, ManagerAlertOut, TaskOut


def cleaner_out(db: Session, cleaner: Cleaner) -> CleanerOut:
    links = db.scalars(select(CleanerProperty).where(CleanerProperty.cleaner_id == cleaner.id)).all()
    return CleanerOut(
        id=cleaner.id,
        name=cleaner.name,
        email=cleaner.email,
        telegram_username=cleaner.telegram_username,
        telegram_user_id=cleaner.telegram_user_id,
        timezone=cleaner.timezone,
        working_days=list(cleaner.working_days or []),
        work_start=cleaner.work_start,
        work_end=cleaner.work_end,
        max_jobs_per_day=cleaner.max_jobs_per_day,
        active=cleaner.active,
        telegram_status=cleaner.telegram_status,
        calendar_status=cleaner.calendar_status,
        unavailable_dates=list(cleaner.unavailable_dates or []),
        property_ids=[link.property_id for link in links if link.restricts],
        preferred_property_ids=[link.property_id for link in links if link.is_preferred],
        created_at=cleaner.created_at,
        updated_at=cleaner.updated_at,
    )


def tasks_out(db: Session, tasks: list[CleaningTask]) -> list[TaskOut]:
    assignment_ids = [task.current_assignment_id for task in tasks if task.current_assignment_id]
    assignments = {}
    if assignment_ids:
        assignments = {
            row.id: row for row in db.scalars(select(Assignment).where(Assignment.id.in_(assignment_ids))).all()
        }
    task_ids = [task.id for task in tasks]
    declined: dict[str, Assignment] = {}
    if task_ids:
        for row in db.scalars(
            select(Assignment)
            .where(Assignment.task_id.in_(task_ids), Assignment.state == AssignmentState.declined)
            .order_by(Assignment.responded_at.desc())
        ).all():
            declined.setdefault(row.task_id, row)
    cleaner_ids = {row.cleaner_id for row in assignments.values()} | {row.cleaner_id for row in declined.values()}
    names = {}
    if cleaner_ids:
        names = {row.id: row.name for row in db.scalars(select(Cleaner).where(Cleaner.id.in_(cleaner_ids))).all()}
    notified: set[str] = set()
    email_status: dict[str, str] = {}
    assignment_mail: dict[str, Notification] = {}
    if task_ids:
        notes = db.scalars(
            select(Notification).where(
                Notification.task_id.in_(task_ids),
                Notification.recipient_type == "host",
                Notification.message_type == "task_unassigned",
                Notification.status == "sent",
            )
        ).all()
        notified = {note.task_id for note in notes if note.task_id}
        deliveries = db.scalars(
            select(NotificationDelivery)
            .where(NotificationDelivery.task_id.in_(task_ids), NotificationDelivery.channel == "email")
            .order_by(NotificationDelivery.created_at.desc())
        ).all()
        for delivery in deliveries:
            if delivery.task_id and delivery.task_id not in email_status:
                email_status[delivery.task_id] = delivery.status
        for note in db.scalars(
            select(Notification)
            .where(
                Notification.task_id.in_(task_ids),
                Notification.channel == "smtp",
                Notification.message_type == "assignment_email",
            )
            .order_by(Notification.created_at.desc())
        ).all():
            if note.task_id and note.task_id not in assignment_mail:
                assignment_mail[note.task_id] = note
    presented = []
    for task in tasks:
        assignment = assignments.get(task.current_assignment_id)
        nested = None
        if assignment is not None:
            nested = AssignmentOut(
                id=assignment.id,
                cleaner_id=assignment.cleaner_id,
                cleaner_name=names.get(assignment.cleaner_id),
                state=assignment.state,
                reason=assignment.reason,
                attempt_number=assignment.attempt_number,
                scheduled_start=assignment.scheduled_start,
                scheduled_end=assignment.scheduled_end,
                deadline_at=assignment.deadline_at,
                responded_at=assignment.responded_at,
            )
        mail = assignment_mail.get(task.id)
        previous = declined.get(task.id)
        replacement = None
        if (
            previous is not None
            and assignment is not None
            and assignment.id != previous.id
            and assignment.state in LIVE_ASSIGNMENT_STATES
        ):
            replacement = names.get(assignment.cleaner_id)
        mail_error = None
        mail_via = None
        if mail is not None and isinstance(mail.provider_response, dict):
            mail_via = mail.provider_response.get("via")
            if mail.status in {"failed", "not_configured"}:
                mail_error = str(mail.provider_response.get("error") or "")[:200] or None
        presented.append(
            TaskOut(
                id=task.id,
                property_id=task.property_id,
                turnover_key=task.turnover_key,
                checkout_reservation_id=task.checkout_reservation_id,
                checkin_reservation_id=task.checkin_reservation_id,
                window_start=task.window_start,
                window_end=task.window_end,
                duration_minutes=task.duration_minutes,
                feed_window_start=task.feed_window_start,
                feed_window_end=task.feed_window_end,
                feed_duration_minutes=task.feed_duration_minutes,
                window_overridden=task.window_overridden,
                status=task.status,
                priority=task.priority,
                assigned_cleaner_id=task.assigned_cleaner_id,
                conflict_details=task.conflict_details,
                manager_notified=task.id in notified,
                email_status=email_status.get(task.id),
                assignment_email_status=mail.status if mail is not None else None,
                assignment_email_via=mail_via if isinstance(mail_via, str) else None,
                assignment_email_error=mail_error,
                rejection_cleaner_name=names.get(previous.cleaner_id) if previous is not None else None,
                replacement_found=True if replacement else (False if previous is not None else None),
                replacement_cleaner_name=replacement,
                started_at=task.started_at,
                host_resolved_at=task.host_resolved_at,
                assignment=nested,
                created_at=task.created_at,
                updated_at=task.updated_at,
            )
        )
    return presented


def decision_out(decision: Decision, manager_alert: ManagerAlertOut | None = None) -> DecisionOut:
    candidates = [
        CandidateOut(
            cleaner_id=item.cleaner_id,
            cleaner_name=item.cleaner_name,
            feasible=item.feasible,
            reasons=list(item.reasons),
            scheduled_start=item.scheduled_start,
            scheduled_end=item.scheduled_end,
            jobs_that_day=item.jobs_that_day,
            preferred=item.preferred,
            slack_minutes=item.slack_seconds // 60,
            summary=describe(item),
        )
        for item in (*decision.ranked_feasible, *decision.rejected)
    ]
    selected = decision.selected.cleaner_id if decision.selected else None
    return DecisionOut(selected_cleaner_id=selected, candidates=candidates, manager_alert=manager_alert)
