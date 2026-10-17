from sqlalchemy import select
from sqlalchemy.orm import Session

from turnoverops.models import Assignment, Cleaner, CleanerProperty, CleaningTask
from turnoverops.scheduling.engine import Decision, describe
from turnoverops.schemas import AssignmentOut, CandidateOut, CleanerOut, DecisionOut, TaskOut


def cleaner_out(db: Session, cleaner: Cleaner) -> CleanerOut:
    links = db.scalars(select(CleanerProperty).where(CleanerProperty.cleaner_id == cleaner.id)).all()
    return CleanerOut(
        id=cleaner.id,
        name=cleaner.name,
        telegram_username=cleaner.telegram_username,
        telegram_user_id=cleaner.telegram_user_id,
        timezone=cleaner.timezone,
        working_days=list(cleaner.working_days or []),
        work_start=cleaner.work_start,
        work_end=cleaner.work_end,
        max_jobs_per_day=cleaner.max_jobs_per_day,
        active=cleaner.active,
        telegram_status=cleaner.telegram_status,
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
    cleaner_ids = {row.cleaner_id for row in assignments.values()}
    names = {}
    if cleaner_ids:
        names = {row.id: row.name for row in db.scalars(select(Cleaner).where(Cleaner.id.in_(cleaner_ids))).all()}
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
        presented.append(
            TaskOut(
                id=task.id,
                property_id=task.property_id,
                turnover_key=task.turnover_key,
                window_start=task.window_start,
                window_end=task.window_end,
                duration_minutes=task.duration_minutes,
                status=task.status,
                priority=task.priority,
                assigned_cleaner_id=task.assigned_cleaner_id,
                conflict_details=task.conflict_details,
                started_at=task.started_at,
                assignment=nested,
                created_at=task.created_at,
                updated_at=task.updated_at,
            )
        )
    return presented


def decision_out(decision: Decision) -> DecisionOut:
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
    return DecisionOut(selected_cleaner_id=selected, candidates=candidates)
