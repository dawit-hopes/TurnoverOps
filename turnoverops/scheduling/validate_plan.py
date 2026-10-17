"""Hard constraints for a whole planning proposal. Nothing is saved until this passes."""

from dataclasses import dataclass
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from turnoverops.scheduling.engine import BusyInterval, CleanerInput, JobWindow, evaluate_cleaner
from turnoverops.scheduling.plan_schema import AssignmentProposal, SchedulePlan
from turnoverops.scheduling.windows import as_utc
from turnoverops.services.planning.policy import TaskView, assignment_feasible


@dataclass(frozen=True)
class ConstraintFailure:
    code: str
    task_id: str | None
    explanation: str

    def as_dict(self) -> dict:
        return {"code": self.code, "task_id": self.task_id, "explanation": self.explanation}


def _slot_in_hours(cleaner: CleanerInput, start, end) -> bool:
    zone = ZoneInfo(cleaner.timezone)
    local_start = as_utc(start).astimezone(zone)
    local_end = as_utc(end).astimezone(zone)
    if local_start.date() != local_end.date():
        return False
    if local_start.weekday() not in cleaner.working_days:
        return False
    if local_start.time() < cleaner.work_start:
        return False
    return local_end.time() <= cleaner.work_end


def _overlaps(start, end, busy: BusyInterval) -> bool:
    return as_utc(start) < as_utc(busy.end) and as_utc(busy.start) < as_utc(end)


def validate_plan(
    plan: SchedulePlan,
    *,
    tasks: dict[str, TaskView],
    cleaners: dict[str, CleanerInput],
    busy: list[BusyInterval],
    expected_run_id: str,
    expected_date: date,
    expected_timezone: str,
) -> list[ConstraintFailure]:
    failures: list[ConstraintFailure] = []
    if plan.planning_run_id != expected_run_id or plan.operational_date != expected_date or plan.timezone != expected_timezone:
        failures.append(
            ConstraintFailure("plan_identity", None, "The plan does not match this planning run, date, or timezone")
        )
    seen: set[str] = set()
    proposals: list[tuple[str, AssignmentProposal]] = [
        * [("create", item) for item in plan.assignments_to_create],
        * [("change", item) for item in plan.assignments_to_change],
    ]
    for bucket in (
        [item.task_id for item in plan.assignments_to_create],
        [item.task_id for item in plan.assignments_to_change],
        [item.task_id for item in plan.assignments_to_preserve],
        [item.task_id for item in plan.unassigned_tasks],
    ):
        for task_id in bucket:
            if task_id in seen and task_id in {item.task_id for item in proposals}:
                pass
    mentioned: list[str] = []
    for _kind, proposal in proposals:
        mentioned.append(proposal.task_id)
    for item in plan.assignments_to_preserve:
        mentioned.append(item.task_id)
    for item in plan.unassigned_tasks:
        mentioned.append(item.task_id)
    duplicates = {task_id for task_id in mentioned if mentioned.count(task_id) > 1}
    for task_id in sorted(duplicates):
        failures.append(ConstraintFailure("duplicate_task", task_id, "The task appears more than once in the plan"))

    changed_ids = {item.task_id for item in plan.assignments_to_change}
    unassigned_ids = {item.task_id for item in plan.unassigned_tasks}
    for task in tasks.values():
        if task.started or task.status == "in_progress":
            if task.id in changed_ids or task.id in unassigned_ids:
                failures.append(
                    ConstraintFailure("locked_assignment", task.id, "In-progress work cannot be moved or unassigned")
                )
            continue
        if assignment_feasible(task, list(cleaners.values()), busy) and (task.id in changed_ids or task.id in unassigned_ids):
            failures.append(
                ConstraintFailure(
                    "still_feasible",
                    task.id,
                    "A feasible confirmed assignment cannot be changed",
                )
            )

    checked: list[AssignmentProposal] = []
    for kind, proposal in proposals:
        if proposal.task_id in duplicates:
            continue
        task = tasks.get(proposal.task_id)
        cleaner = cleaners.get(proposal.cleaner_id)
        if task is None:
            failures.append(ConstraintFailure("invalid_task", proposal.task_id, "Unknown task id"))
            continue
        if cleaner is None:
            failures.append(ConstraintFailure("invalid_cleaner", proposal.task_id, "Unknown cleaner id"))
            continue
        if kind == "change" and (task.started or task.status == "in_progress"):
            continue
        duration = timedelta(minutes=task.duration_minutes)
        if as_utc(proposal.end) - as_utc(proposal.start) != duration:
            failures.append(ConstraintFailure("duration", task.id, "The proposed slot does not match the cleaning duration"))
        if as_utc(proposal.start) < as_utc(task.window_start) or as_utc(proposal.end) > as_utc(task.window_end):
            failures.append(ConstraintFailure("deadline", task.id, "The proposed slot falls outside the turnover window"))
        elif as_utc(proposal.end) > as_utc(proposal.deadline) or as_utc(proposal.deadline) > as_utc(task.window_end):
            failures.append(ConstraintFailure("deadline", task.id, "The proposal deadline is outside the turnover window"))
        window = JobWindow(
            property_id=task.property_id,
            property_timezone=task.property_timezone,
            window_start=task.window_start,
            window_end=task.window_end,
            duration_minutes=task.duration_minutes,
        )
        structural = evaluate_cleaner(cleaner, window, [])
        for reason in structural.reasons:
            if reason in {"inactive", "unavailable", "not_eligible", "insufficient_window", "outside_working_hours"}:
                failures.append(ConstraintFailure(reason, task.id, f"{cleaner.name} cannot take this task: {reason}"))
        if structural.feasible and not _slot_in_hours(cleaner, proposal.start, proposal.end):
            failures.append(
                ConstraintFailure(
                    "outside_working_hours",
                    task.id,
                    f"{cleaner.name} is not working between {proposal.start.isoformat()} and {proposal.end.isoformat()}",
                )
            )
        checked.append(proposal)

    preserved_busy = [
        item
        for item in busy
        if item.task_id not in {proposal.task_id for proposal in checked}
    ]
    proposal_busy = [
        BusyInterval(proposal.cleaner_id, proposal.task_id, proposal.start, proposal.end) for proposal in checked
    ]
    combined = preserved_busy + proposal_busy
    for proposal in checked:
        cleaner = cleaners[proposal.cleaner_id]
        task = tasks[proposal.task_id]
        zone = ZoneInfo(task.property_timezone)
        day = as_utc(proposal.start).astimezone(zone).date()
        jobs = 0
        for item in combined:
            if item.cleaner_id != proposal.cleaner_id or item.task_id == proposal.task_id:
                continue
            if _overlaps(proposal.start, proposal.end, item):
                failures.append(
                    ConstraintFailure("overlap", task.id, f"{cleaner.name} is already booked during this slot")
                )
                break
            if as_utc(item.start).astimezone(zone).date() == day:
                jobs += 1
        else:
            if cleaner.max_jobs_per_day is not None and jobs >= cleaner.max_jobs_per_day:
                failures.append(
                    ConstraintFailure("daily_job_limit", task.id, f"{cleaner.name} is at the daily job limit")
                )
    return failures
