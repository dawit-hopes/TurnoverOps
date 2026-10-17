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
from turnoverops.models import Assignment, Cleaner, CleaningTask, Property
from turnoverops.scheduling.engine import Decision, Evaluation, describe
from turnoverops.services.audit import audit
from turnoverops.services.evaluation import evaluate_window, task_window
from turnoverops.services.notifications import notify_cleaner, notify_host


def bump(db: Session, task: CleaningTask) -> None:
    expected = task.assignment_version
    result = db.execute(
        update(CleaningTask)
        .where(CleaningTask.id == task.id, CleaningTask.assignment_version == expected)
        .values(assignment_version=expected + 1)
    )
    if result.rowcount != 1:
        raise AppError(409, "conflict", "The task was updated by another worker")
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
    task.last_evaluated_at = now
    task.conflict_details = {
        "summary": summary,
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
    notify_host(db, task.host_id, event_type, f"{prop.name}: {summary}")


def _commit_proposal(
    db: Session,
    task: CleaningTask,
    prop: Property,
    chosen: Evaluation,
    decision: Decision,
    now: datetime,
    settings: Settings,
    *,
    attempt_number: int,
    actor: str,
) -> Assignment:
    assignment = Assignment(
        host_id=task.host_id,
        task_id=task.id,
        cleaner_id=chosen.cleaner_id,
        state=AssignmentState.awaiting_confirmation,
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
    )
    db.add(assignment)
    db.flush()
    task.assigned_cleaner_id = chosen.cleaner_id
    task.current_assignment_id = assignment.id
    task.status = TaskStatus.awaiting_confirmation
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
        details={"cleaner_id": chosen.cleaner_id, "actor": actor, "attempt": attempt_number},
    )
    cleaner = db.get(Cleaner, chosen.cleaner_id)
    if cleaner is not None:
        notify_cleaner(
            db,
            cleaner,
            "assignment_proposed",
            f"{prop.name} needs cleaning from {chosen.scheduled_start.isoformat()} to {chosen.scheduled_end.isoformat()}. Reply accept or decline.",
            settings,
            now=now,
        )
    notify_host(
        db,
        task.host_id,
        "assignment_proposed",
        f"Proposed {chosen.cleaner_name} for {prop.name}. Waiting for confirmation.",
    )
    return assignment


def _propose(
    db: Session,
    task: CleaningTask,
    prop: Property,
    decision: Decision,
    now: datetime,
    settings: Settings,
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
            raise AppError(409, "infeasible", "Cleaner cannot take this task", details=reasons)
    else:
        chosen = next((item for item in decision.ranked_feasible if item.cleaner_id not in excluded), None)
        if chosen is None:
            summary = "No feasible cleaner can take this turnover"
            _attention(
                db,
                task,
                prop,
                settings,
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
        attempt_number=len(failures) + 1,
        actor=actor,
    )


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
        task.assigned_cleaner_id = None
        task.current_assignment_id = None
        audit(
            db,
            host_id=task.host_id,
            event_type="conflict_detected",
            entity_type="cleaning_task",
            entity_id=task.id,
            reason=describe(evaluation) if evaluation else "The assigned cleaner is no longer available",
            details={"cleaner_id": current.cleaner_id},
        )
        _propose(db, task, prop, decision, now, settings, ignore_cap=ignore_cap, cleaner_id=None, extra_excluded=set(), actor=actor)
        return
    bump(db, task)
    _propose(
        db,
        task,
        prop,
        decision,
        now,
        settings,
        ignore_cap=ignore_cap,
        cleaner_id=cleaner_id,
        extra_excluded=set(),
        actor=actor,
    )


def reconcile_open(db: Session, prop: Property, now: datetime, settings: Settings) -> list[CleaningTask]:
    tasks = db.scalars(
        select(CleaningTask).where(
            CleaningTask.property_id == prop.id,
            CleaningTask.status.notin_((TaskStatus.completed, TaskStatus.blocked)),
        )
    ).all()
    for task in tasks:
        try:
            reconcile_task(db, task, prop, now, settings)
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
        details={"actor": actor, "cleaner_id": assignment.cleaner_id},
    )
    notify_host(db, task.host_id, "assignment_accepted", f"{prop.name} cleaning is confirmed.")
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
) -> CleaningTask:
    task = db.get(CleaningTask, assignment.task_id)
    prop = db.get(Property, task.property_id) if task else None
    if task is None or prop is None:
        raise AppError(404, "not_found", "Not found")
    if task.started_at is not None or task.status in LOCKED_TASK_STATUSES:
        raise AppError(409, "task_locked", "This task can no longer be reassigned automatically")
    if assignment.state != AssignmentState.awaiting_confirmation or task.current_assignment_id != assignment.id:
        raise AppError(409, "invalid_state", "This assignment is not awaiting confirmation")
    bump(db, task)
    assignment.state = state
    assignment.responded_at = now
    task.assigned_cleaner_id = None
    task.current_assignment_id = None
    task.status = TaskStatus.unassigned
    event_type = "assignment_declined" if state == AssignmentState.declined else "assignment_expired"
    audit(
        db,
        host_id=task.host_id,
        event_type=event_type,
        entity_type="assignment",
        entity_id=assignment.id,
        reason=reason,
        details={"actor": actor, "cleaner_id": assignment.cleaner_id},
    )
    decision = evaluate_window(db, task.host_id, task_window(task, prop.timezone), exclude_task_id=task.id)
    _propose(db, task, prop, decision, now, settings, ignore_cap=False, cleaner_id=None, extra_excluded=set(), actor="system")
    return task


def reassign_task(
    db: Session,
    task: CleaningTask,
    prop: Property,
    now: datetime,
    settings: Settings,
    *,
    cleaner_id: str | None,
) -> CleaningTask:
    if task.started_at is not None or task.status in LOCKED_TASK_STATUSES:
        raise AppError(409, "task_locked", "This task can no longer be reassigned automatically")
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
            raise AppError(409, "infeasible", "Cleaner cannot take this task", details=reasons)
    elif not any(item.cleaner_id not in extra_excluded for item in decision.ranked_feasible):
        raise AppError(409, "infeasible", "No other feasible cleaner is available")
    bump(db, task)
    if current is not None and current.state in LIVE_ASSIGNMENT_STATES:
        current.state = AssignmentState.cancelled
        current.responded_at = now
        audit(
            db,
            host_id=task.host_id,
            event_type="assignment_cancelled",
            entity_type="assignment",
            entity_id=current.id,
            reason="Host reassigned the task",
            details={"actor": "host"},
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
        ignore_cap=True,
        cleaner_id=cleaner_id,
        extra_excluded=extra_excluded,
        actor="host",
    )
    return task


def retry_task(db: Session, task: CleaningTask, prop: Property, now: datetime, settings: Settings) -> CleaningTask:
    if task.started_at is not None or task.status in LOCKED_TASK_STATUSES:
        raise AppError(409, "task_locked", "This task can no longer be reassigned automatically")
    reconcile_task(db, task, prop, now, settings, ignore_cap=True, actor="host")
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
    notify_host(db, task.host_id, "cleaning_completed", f"{prop.name} cleaning is marked complete.")
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
    notify_host(db, task.host_id, "delay_reported", f"{prop.name}: the cleaner reported a delay.")


def report_unavailability(
    db: Session,
    task: CleaningTask,
    prop: Property,
    now: datetime,
    settings: Settings,
    *,
    actor: str,
) -> None:
    if task.status == TaskStatus.completed:
        raise AppError(409, "invalid_state", "Completed work cannot be changed")
    if task.started_at is not None or task.status == TaskStatus.in_progress:
        bump(db, task)
        _attention(
            db,
            task,
            prop,
            settings,
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
    )


def expire_due_assignments(db: Session, settings: Settings, now: datetime) -> int:
    rows = db.scalars(
        select(Assignment).where(
            Assignment.state == AssignmentState.awaiting_confirmation,
            Assignment.deadline_at <= now,
        )
    ).all()
    expired = 0
    for assignment in rows:
        task = db.get(CleaningTask, assignment.task_id)
        if task is None or task.current_assignment_id != assignment.id:
            continue
        if task.status != TaskStatus.awaiting_confirmation:
            continue
        try:
            close_assignment(
                db,
                assignment,
                now,
                settings,
                state=AssignmentState.expired,
                actor="system",
                reason="Confirmation deadline expired",
            )
        except AppError as exc:
            if exc.code != "conflict":
                raise
            continue
        expired += 1
    return expired
