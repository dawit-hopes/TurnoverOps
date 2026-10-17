import hashlib
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from turnoverops.core.config import Settings
from turnoverops.core.errors import AppError
from turnoverops.domain import (
    LIVE_ASSIGNMENT_STATES,
    LOCKED_TASK_STATUSES,
    AssignmentState,
    TaskStatus,
)
from turnoverops.models import Assignment, Cleaner, CleaningTask, Host, Property
from turnoverops.scheduling.engine import Decision, Evaluation, describe
from turnoverops.services.audit import audit
from turnoverops.services.evaluation import evaluate_window, task_window
from turnoverops.services.host_copy import plain_brief
from turnoverops.services.host_policy import HostPolicy, load_policy
from turnoverops.services.notifications import notify_host
from turnoverops.services.planning.alerts import alert_unassigned


def _revoke_links(db: Session, assignment_id: str, now: datetime) -> None:
    from turnoverops.services.cleaner_mail import revoke_rejection_tokens

    revoke_rejection_tokens(db, assignment_id, now)


def bump(db: Session, task: CleaningTask) -> None:
    expected = task.assignment_version
    result = db.execute(
        update(CleaningTask)
        .where(CleaningTask.id == task.id, CleaningTask.assignment_version == expected)
        .values(assignment_version=expected + 1)
    )
    if result.rowcount != 1:
        raise AppError(409, "conflict", "This cleaning changed while you were working. Refresh and try again.")
    task.assignment_version = expected + 1


def current_assignment(db: Session, task: CleaningTask) -> Assignment | None:
    if not task.current_assignment_id:
        return None
    return db.get(Assignment, task.current_assignment_id)


def _find(decision: Decision, cleaner_id: str) -> Evaluation | None:
    for item in (*decision.ranked_feasible, *decision.rejected):
        if item.cleaner_id == cleaner_id:
            return item
    return None


def _signature(summary: str, task: CleaningTask) -> str:
    raw = f"{summary}|{task.window_start.isoformat()}|{task.window_end.isoformat()}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _window_failures(db: Session, task: CleaningTask) -> list[Assignment]:
    rows = db.scalars(
        select(Assignment).where(
            Assignment.task_id == task.id,
            Assignment.state.in_((AssignmentState.declined, AssignmentState.expired)),
        )
    ).all()
    return [row for row in rows if row.window_start == task.window_start and row.window_end == task.window_end]


def _record_rejections(db: Session, task: CleaningTask, decision: Decision) -> None:
    for item in decision.rejected:
        audit(
            db,
            host_id=task.host_id,
            event_type="candidate_rejected",
            entity_type="cleaning_task",
            entity_id=task.id,
            reason=describe(item),
            details={"cleaner_id": item.cleaner_id, "reasons": list(item.reasons)},
        )


def _attention(
    db: Session,
    task: CleaningTask,
    prop: Property,
    settings: Settings,
    policy: HostPolicy,
    now: datetime,
    summary: str,
    *,
    event_type: str,
    clear_assignee: bool,
    decision: Decision | None = None,
) -> None:
    signature = _signature(summary, task)
    already = task.status == TaskStatus.at_risk and (task.conflict_details or {}).get("signature") == signature
    task.status = TaskStatus.at_risk
    task.host_resolved_at = None
    task.last_evaluated_at = now
    task.conflict_details = {
        "summary": summary,
        "brief": plain_brief(summary),
        "signature": signature,
        "rejected": [
            {"cleaner_id": item.cleaner_id, "reasons": list(item.reasons)} for item in (decision.rejected if decision else ())
        ],
    }
    if clear_assignee:
        task.assigned_cleaner_id = None
        task.current_assignment_id = None
    if already:
        return
    if decision is not None:
        _record_rejections(db, task, decision)
    audit(
        db,
        host_id=task.host_id,
        event_type=event_type,
        entity_type="cleaning_task",
        entity_id=task.id,
        reason=summary,
        details={"signature": signature},
    )
    if not policy.notify_host_on_escalation:
        return
    no_coverage = clear_assignee and decision is not None and not decision.ranked_feasible
    host = db.get(Host, task.host_id) if no_coverage else None
    if host is not None:
        cleaner_ids = [item.cleaner_id for item in (*decision.ranked_feasible, *decision.rejected)] if decision else []
        alert_unassigned(
            db,
            host,
            task,
            prop,
            settings,
            reason=summary,
            cleaner_ids=cleaner_ids,
        )
        return
    notify_host(db, task.host_id, event_type, f"{prop.name}: {summary}", task_id=task.id)


def _commit_proposal(
    db: Session,
    task: CleaningTask,
    prop: Property,
    chosen: Evaluation,
    decision: Decision,
    now: datetime,
    settings: Settings,
    policy: HostPolicy,
    *,
    attempt_number: int,
    actor: str,
) -> Assignment:
    assignment = Assignment(
        host_id=task.host_id,
        task_id=task.id,
        cleaner_id=chosen.cleaner_id,
        state=AssignmentState.accepted,
        reason=describe(chosen),
        explanation={
            "jobs_that_day": chosen.jobs_that_day,
            "preferred": chosen.preferred,
            "slack_minutes": chosen.slack_seconds // 60,
            "reasons": [],
            "actor": actor,
        },
        attempt_number=attempt_number,
        window_start=task.window_start,
        window_end=task.window_end,
        scheduled_start=chosen.scheduled_start,
        scheduled_end=chosen.scheduled_end,
        proposed_at=now,
        deadline_at=now + timedelta(minutes=settings.confirmation_timeout_minutes),
        responded_at=now,
    )
    db.add(assignment)
    db.flush()
    task.assigned_cleaner_id = chosen.cleaner_id
    task.current_assignment_id = assignment.id
    task.status = TaskStatus.confirmed
    task.last_evaluated_at = now
    task.conflict_details = {
        "summary": describe(chosen),
        "selected_cleaner_id": chosen.cleaner_id,
        "rejected": [{"cleaner_id": item.cleaner_id, "reasons": list(item.reasons)} for item in decision.rejected],
    }
    _record_rejections(db, task, decision)
    audit(
        db,
        host_id=task.host_id,
        event_type="assignment_proposed",
        entity_type="assignment",
        entity_id=assignment.id,
        reason=describe(chosen),
        details={"cleaner_id": chosen.cleaner_id, "actor": actor, "attempt": attempt_number, "task_id": task.id},
    )
    if policy.notify_cleaners:
        from turnoverops.services.cleaner_mail import send_assignment_email

        send_assignment_email(db, assignment, settings, now)
    audit(
        db,
        host_id=task.host_id,
        event_type="assignment_accepted",
        entity_type="assignment",
        entity_id=assignment.id,
        reason="Assignment is confirmed until the cleaner declines",
        details={"actor": "system", "cleaner_id": chosen.cleaner_id, "task_id": task.id},
    )
    notify_host(
        db,
        task.host_id,
        "assignment_accepted",
        f"{prop.name} cleaning is confirmed for {chosen.cleaner_name}.",
        task_id=task.id,
    )
    from turnoverops.services.calendar_sync import publish_task

    publish_task(db, task, prop, settings)
    return assignment


def _propose(
    db: Session,
    task: CleaningTask,
    prop: Property,
    decision: Decision,
    now: datetime,
    settings: Settings,
    policy: HostPolicy,
    *,
    ignore_cap: bool,
    cleaner_id: str | None,
    extra_excluded: set[str],
    actor: str,
) -> Assignment | None:
    failures = _window_failures(db, task)
    if cleaner_id is None and not ignore_cap and len(failures) >= settings.max_assignment_attempts:
        _attention(
            db,
            task,
            prop,
            settings,
            policy,
            now,
            "Assignment retry limit reached",
            event_type="recovery_exhausted",
            clear_assignee=True,
            decision=decision,
        )
        return None
    excluded = {row.cleaner_id for row in failures} | extra_excluded
    if cleaner_id is not None:
        chosen = next((item for item in decision.ranked_feasible if item.cleaner_id == cleaner_id), None)
        if chosen is None:
            rejected = _find(decision, cleaner_id)
            reasons = list(rejected.reasons) if rejected else ["not_eligible"]
            raise AppError(409, "infeasible", "This cleaner cannot take this cleaning in the available time.", details=reasons)
    else:
        chosen = next((item for item in decision.ranked_feasible if item.cleaner_id not in excluded), None)
        if chosen is None:
            summary = "No feasible cleaner can take this turnover"
            _attention(
                db,
                task,
                prop,
                settings,
                policy,
                now,
                summary,
                event_type="recovery_exhausted",
                clear_assignee=True,
                decision=decision,
            )
            return None
    return _commit_proposal(
        db,
        task,
        prop,
        chosen,
        decision,
        now,
        settings,
        policy,
        attempt_number=len(failures) + 1,
        actor=actor,
    )


def _park_for_host(
    db: Session,
    task: CleaningTask,
    prop: Property,
    policy: HostPolicy,
    now: datetime,
    summary: str,
) -> None:
    task.status = TaskStatus.at_risk
    task.host_resolved_at = None
    task.last_evaluated_at = now
    task.assigned_cleaner_id = None
    task.current_assignment_id = None
    task.conflict_details = {
        "summary": summary,
        "brief": plain_brief(summary),
        "signature": _signature(summary, task),
        "rejected": [],
    }
    audit(
        db,
        host_id=task.host_id,
        event_type="host_intervention_required",
        entity_type="cleaning_task",
        entity_id=task.id,
        reason=summary,
        details={"auto_reassignment": False},
    )
    if policy.notify_host_on_escalation:
        notify_host(db, task.host_id, "host_intervention_required", f"{prop.name}: {summary}", task_id=task.id)


def reconcile_task(
    db: Session,
    task: CleaningTask,
    prop: Property,
    now: datetime,
    settings: Settings,
    *,
    ignore_cap: bool = False,
    cleaner_id: str | None = None,
    actor: str = "system",
    policy: HostPolicy,
) -> None:
    if task.status in {TaskStatus.completed, TaskStatus.blocked}:
        return
    decision = evaluate_window(db, task.host_id, task_window(task, prop.timezone), exclude_task_id=task.id)
    task.last_evaluated_at = now
    current = current_assignment(db, task)
    if task.started_at is not None:
        evaluation = _find(decision, current.cleaner_id) if current else None
        if evaluation is None or not evaluation.feasible:
            bump(db, task)
            _attention(
                db,
                task,
                prop,
                settings,
                policy,
                now,
                "Cleaning is in progress and can no longer be finished in the updated window",
                event_type="host_intervention_required",
                clear_assignee=False,
                decision=decision,
            )
        return
    if cleaner_id is None and current is not None and current.state in LIVE_ASSIGNMENT_STATES:
        evaluation = _find(decision, current.cleaner_id)
        if evaluation is not None and evaluation.feasible:
            if task.status == TaskStatus.at_risk and current.state == AssignmentState.accepted:
                task.status = TaskStatus.confirmed
            task.conflict_details = {"summary": "Existing assignment remains feasible", "cleaner_id": current.cleaner_id}
            return
        bump(db, task)
        current.state = AssignmentState.cancelled
        current.responded_at = now
        _revoke_links(db, current.id, now)
        task.assigned_cleaner_id = None
        task.current_assignment_id = None
        lost = describe(evaluation) if evaluation else "The assigned cleaner is no longer available"
        audit(
            db,
            host_id=task.host_id,
            event_type="conflict_detected",
            entity_type="cleaning_task",
            entity_id=task.id,
            reason=lost,
            details={"cleaner_id": current.cleaner_id, "task_id": task.id},
        )
        task.host_resolved_at = None
        if not policy.auto_reassignment and actor != "host" and cleaner_id is None:
            _park_for_host(db, task, prop, policy, now, lost)
            return
        _propose(
            db,
            task,
            prop,
            decision,
            now,
            settings,
            policy,
            ignore_cap=ignore_cap,
            cleaner_id=None,
            extra_excluded=set(),
            actor=actor,
        )
        return
    bump(db, task)
    _propose(
        db,
        task,
        prop,
        decision,
        now,
        settings,
        policy,
        ignore_cap=ignore_cap,
        cleaner_id=cleaner_id,
        extra_excluded=set(),
        actor=actor,
    )


def reconcile_open(db: Session, prop: Property, now: datetime, settings: Settings) -> list[CleaningTask]:
    policy = load_policy(db, prop.host_id)
    tasks = db.scalars(
        select(CleaningTask).where(
            CleaningTask.property_id == prop.id,
            CleaningTask.status.notin_((TaskStatus.completed, TaskStatus.blocked)),
        )
    ).all()
    for task in tasks:
        try:
            reconcile_task(db, task, prop, now, settings, policy=policy)
        except AppError as exc:
            if exc.code != "conflict":
                raise
    return list(tasks)


def accept_assignment(db: Session, assignment: Assignment, now: datetime, settings: Settings, *, actor: str) -> CleaningTask:
    task = db.get(CleaningTask, assignment.task_id)
    prop = db.get(Property, task.property_id) if task else None
    if task is None or prop is None or task.host_id != assignment.host_id:
        raise AppError(404, "not_found", "Not found")
    if assignment.state != AssignmentState.awaiting_confirmation or task.status != TaskStatus.awaiting_confirmation:
        raise AppError(409, "invalid_state", "This assignment is not awaiting confirmation")
    if task.current_assignment_id != assignment.id:
        raise AppError(409, "invalid_state", "This assignment is no longer current")
    bump(db, task)
    assignment.state = AssignmentState.accepted
    assignment.responded_at = now
    task.status = TaskStatus.confirmed
    audit(
        db,
        host_id=task.host_id,
        event_type="assignment_accepted",
        entity_type="assignment",
        entity_id=assignment.id,
        reason="Cleaner accepted the assignment" if actor == "cleaner" else "Assignment accepted",
        details={"actor": actor, "cleaner_id": assignment.cleaner_id, "task_id": task.id},
    )
    notify_host(db, task.host_id, "assignment_accepted", f"{prop.name} cleaning is confirmed.", task_id=task.id)
    from turnoverops.services.calendar_sync import publish_task

    publish_task(db, task, prop, settings)
    return task


def close_assignment(
    db: Session,
    assignment: Assignment,
    now: datetime,
    settings: Settings,
    *,
    state: AssignmentState,
    actor: str,
    reason: str,
    policy: HostPolicy | None = None,
) -> CleaningTask:
    task = db.get(CleaningTask, assignment.task_id)
    prop = db.get(Property, task.property_id) if task else None
    if task is None or prop is None:
        raise AppError(404, "not_found", "Not found")
    host_policy = policy or load_policy(db, task.host_id)
    if task.started_at is not None or task.status in LOCKED_TASK_STATUSES:
        raise AppError(409, "task_locked", "This cleaning can no longer be reassigned automatically.")
    if task.current_assignment_id != assignment.id:
        raise AppError(409, "invalid_state", "This assignment is no longer current")
    waiting = assignment.state == AssignmentState.awaiting_confirmation and task.status == TaskStatus.awaiting_confirmation
    accepted = assignment.state == AssignmentState.accepted and task.status == TaskStatus.confirmed and task.started_at is None
    if not waiting and not accepted:
        raise AppError(409, "invalid_state", "This assignment can no longer be declined")
    bump(db, task)
    assignment.state = state
    assignment.responded_at = now
    _revoke_links(db, assignment.id, now)
    task.assigned_cleaner_id = None
    task.current_assignment_id = None
    task.status = TaskStatus.unassigned
    task.host_resolved_at = None
    event_type = "assignment_declined" if state == AssignmentState.declined else "assignment_expired"
    audit(
        db,
        host_id=task.host_id,
        event_type=event_type,
        entity_type="assignment",
        entity_id=assignment.id,
        reason=reason,
        details={"actor": actor, "cleaner_id": assignment.cleaner_id, "task_id": task.id},
    )
    if not host_policy.auto_reassignment:
        _park_for_host(db, task, prop, host_policy, now, reason)
        return task
    decision = evaluate_window(db, task.host_id, task_window(task, prop.timezone), exclude_task_id=task.id)
    _propose(
        db,
        task,
        prop,
        decision,
        now,
        settings,
        host_policy,
        ignore_cap=False,
        cleaner_id=None,
        extra_excluded=set(),
        actor="system",
    )
    return task


def reassign_task(
    db: Session,
    task: CleaningTask,
    prop: Property,
    now: datetime,
    settings: Settings,
    *,
    cleaner_id: str | None,
    policy: HostPolicy | None = None,
) -> CleaningTask:
    host_policy = policy or load_policy(db, task.host_id)
    if task.started_at is not None or task.status in LOCKED_TASK_STATUSES:
        raise AppError(409, "task_locked", "This cleaning can no longer be reassigned automatically.")
    decision = evaluate_window(db, task.host_id, task_window(task, prop.timezone), exclude_task_id=task.id)
    current = current_assignment(db, task)
    extra_excluded: set[str] = set()
    if cleaner_id is None and current is not None:
        extra_excluded.add(current.cleaner_id)
    if cleaner_id is not None:
        chosen = next((item for item in decision.ranked_feasible if item.cleaner_id == cleaner_id), None)
        if chosen is None:
            rejected = _find(decision, cleaner_id)
            reasons = list(rejected.reasons) if rejected else ["not_eligible"]
            raise AppError(409, "infeasible", "This cleaner cannot take this cleaning in the available time.", details=reasons)
    elif not any(item.cleaner_id not in extra_excluded for item in decision.ranked_feasible):
        raise AppError(409, "infeasible", "No other feasible cleaner is available")
    bump(db, task)
    if current is not None and current.state in LIVE_ASSIGNMENT_STATES:
        current.state = AssignmentState.cancelled
        current.responded_at = now
        _revoke_links(db, current.id, now)
        audit(
            db,
            host_id=task.host_id,
            event_type="assignment_cancelled",
            entity_type="assignment",
            entity_id=current.id,
            reason="Host reassigned the task",
            details={"actor": "host", "task_id": task.id},
        )
    task.assigned_cleaner_id = None
    task.current_assignment_id = None
    _propose(
        db,
        task,
        prop,
        decision,
        now,
        settings,
        host_policy,
        ignore_cap=True,
        cleaner_id=cleaner_id,
        extra_excluded=extra_excluded,
        actor="host",
    )
    return task


def retry_task(db: Session, task: CleaningTask, prop: Property, now: datetime, settings: Settings) -> CleaningTask:
    if task.started_at is not None or task.status in LOCKED_TASK_STATUSES:
        raise AppError(409, "task_locked", "This cleaning can no longer be reassigned automatically.")
    reconcile_task(db, task, prop, now, settings, ignore_cap=True, actor="host", policy=load_policy(db, task.host_id))
    return task


def start_cleaning(db: Session, task: CleaningTask, now: datetime, *, actor: str) -> CleaningTask:
    if task.status != TaskStatus.confirmed:
        raise AppError(409, "invalid_state", "Cleaning can only start from a confirmed task")
    bump(db, task)
    task.started_at = now
    task.status = TaskStatus.in_progress
    audit(
        db,
        host_id=task.host_id,
        event_type="cleaning_started",
        entity_type="cleaning_task",
        entity_id=task.id,
        reason="Cleaning started",
        details={"actor": actor},
    )
    return task


def complete_cleaning(db: Session, task: CleaningTask, prop: Property, now: datetime, settings: Settings, *, actor: str) -> CleaningTask:
    if task.status not in {TaskStatus.confirmed, TaskStatus.in_progress, TaskStatus.at_risk} or (
        task.status == TaskStatus.at_risk and task.started_at is None
    ):
        raise AppError(409, "invalid_state", "Cleaning can only be completed after it is confirmed")
    bump(db, task)
    task.status = TaskStatus.completed
    audit(
        db,
        host_id=task.host_id,
        event_type="cleaning_completed",
        entity_type="cleaning_task",
        entity_id=task.id,
        reason="Cleaning completed",
        details={"actor": actor},
    )
    notify_host(db, task.host_id, "cleaning_completed", f"{prop.name} cleaning is marked complete.", task_id=task.id)
    from turnoverops.services.calendar_sync import publish_task

    publish_task(db, task, prop, settings)
    return task


def report_delay(db: Session, task: CleaningTask, prop: Property, *, actor: str) -> None:
    if task.status not in {TaskStatus.confirmed, TaskStatus.in_progress}:
        raise AppError(409, "invalid_state", "A delay can only be reported on an active task")
    audit(
        db,
        host_id=task.host_id,
        event_type="delay_reported",
        entity_type="cleaning_task",
        entity_id=task.id,
        reason="Cleaner reported a delay",
        details={"actor": actor},
    )
    notify_host(db, task.host_id, "delay_reported", f"{prop.name}: the cleaner reported a delay.", task_id=task.id)


def report_unavailability(
    db: Session,
    task: CleaningTask,
    prop: Property,
    now: datetime,
    settings: Settings,
    *,
    actor: str,
    policy: HostPolicy | None = None,
) -> None:
    host_policy = policy or load_policy(db, task.host_id)
    if task.status == TaskStatus.completed:
        raise AppError(409, "invalid_state", "Completed work cannot be changed")
    if task.started_at is not None or task.status == TaskStatus.in_progress:
        bump(db, task)
        _attention(
            db,
            task,
            prop,
            settings,
            host_policy,
            now,
            "Cleaner reported they cannot finish work already in progress",
            event_type="host_intervention_required",
            clear_assignee=False,
        )
        return
    assignment = current_assignment(db, task)
    if assignment is None:
        raise AppError(409, "invalid_state", "This task has no current assignment")
    close_assignment(
        db,
        assignment,
        now,
        settings,
        state=AssignmentState.declined,
        actor=actor,
        reason="Cleaner reported they are unavailable",
        policy=host_policy,
    )


def mark_cleaner_unavailable(db: Session, task: CleaningTask, prop: Property, now: datetime, settings: Settings) -> CleaningTask:
    if task.started_at is not None or task.status in LOCKED_TASK_STATUSES:
        raise AppError(409, "task_locked", "This cleaning can no longer be reassigned automatically.")
    assignment = current_assignment(db, task)
    if assignment is None:
        raise AppError(409, "invalid_state", "This task has no current assignment")
    cleaner = db.get(Cleaner, assignment.cleaner_id)
    if cleaner is None:
        raise AppError(404, "not_found", "Not found")
    from zoneinfo import ZoneInfo

    day = task.window_start.astimezone(ZoneInfo(prop.timezone)).date().isoformat()
    dates = [item for item in (cleaner.unavailable_dates or []) if item != day]
    dates.append(day)
    cleaner.unavailable_dates = dates
    audit(
        db,
        host_id=task.host_id,
        event_type="cleaner_unavailable",
        entity_type="cleaner",
        entity_id=cleaner.id,
        reason=f"{cleaner.name} is unavailable on {day}",
        details={"date": day, "task_id": task.id},
    )
    policy = load_policy(db, task.host_id)
    report_unavailability(db, task, prop, now, settings, actor="host", policy=policy)
    others = db.scalars(
        select(CleaningTask).where(
            CleaningTask.host_id == task.host_id,
            CleaningTask.id != task.id,
            CleaningTask.assigned_cleaner_id == cleaner.id,
            CleaningTask.status.notin_((TaskStatus.completed, TaskStatus.blocked, TaskStatus.in_progress)),
        )
    ).all()
    for other in others:
        other_prop = db.get(Property, other.property_id)
        if other_prop is None or other.started_at is not None:
            continue
        other_day = other.window_start.astimezone(ZoneInfo(other_prop.timezone)).date().isoformat()
        if other_day != day:
            continue
        try:
            reconcile_task(db, other, other_prop, now, settings, policy=policy)
        except AppError as exc:
            if exc.code != "conflict":
                raise
    return task


def resolve_issue(db: Session, task: CleaningTask, now: datetime) -> CleaningTask:
    task.host_resolved_at = now
    audit(
        db,
        host_id=task.host_id,
        event_type="issue_resolved",
        entity_type="cleaning_task",
        entity_id=task.id,
        reason="Host marked the issue resolved",
    )
    return task


def confirm_waiting_assignments(db: Session, now: datetime, host_id: str | None = None) -> int:
    stmt = select(Assignment).where(Assignment.state == AssignmentState.awaiting_confirmation)
    if host_id is not None:
        stmt = stmt.where(Assignment.host_id == host_id)
    confirmed = 0
    for assignment in db.scalars(stmt).all():
        task = db.get(CleaningTask, assignment.task_id)
        if task is None or task.current_assignment_id != assignment.id or task.status != TaskStatus.awaiting_confirmation:
            continue
        if assignment.source in {"daily_planning", "calendar_change", "fast_path"} and assignment.dispatched_at is None:
            continue
        assignment.state = AssignmentState.accepted
        assignment.responded_at = now
        task.status = TaskStatus.confirmed
        audit(
            db,
            host_id=task.host_id,
            event_type="assignment_accepted",
            entity_type="assignment",
            entity_id=assignment.id,
            reason="Assignment is confirmed until the cleaner declines",
            details={"actor": "system", "cleaner_id": assignment.cleaner_id, "task_id": task.id},
        )
        confirmed += 1
    return confirmed


def expire_due_assignments(db: Session, settings: Settings, now: datetime) -> int:
    """Confirm due replies. Undispatched planning rows stay put until a host approves automatic dispatch."""
    return confirm_waiting_assignments(db, now)
