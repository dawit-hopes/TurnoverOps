"""When the scheduling model is called.

Skip the model when every relevant task is completed, in progress and still feasible,
or already validly assigned, and the scope fingerprint matches the last validated run.
Duplicate calendar events and proven-impossible cases also skip the model: if
evaluate_cleaner shows no eligible cleaner and no movable assignment, escalate the
task instead of inventing a cleaner.

Fast path, no model, only when allow_fast_path is on, the scope is a single task that
needs a decision, decide() returns one feasible cleaner, and that cleaner has no other
open task in the horizon that also needs them. The fast path is the deterministic
ranker in scheduling/engine.py.

Call the agent when two or more open tasks can use the same cleaner, a reservation
change makes an existing assignment infeasible and another assignment might move, or
the validator rejected a proposal. One primary call per connected scope, then at most
SCHEDULING_MAX_REPAIR_ATTEMPTS compact repair calls.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime

from turnoverops.domain import LIVE_ASSIGNMENT_STATES
from turnoverops.scheduling.engine import BusyInterval, CleanerInput, Evaluation, JobWindow, decide

PLANNING_SOURCES = frozenset({"daily_planning", "calendar_change", "fast_path"})
SKIP_UNCHANGED = "unchanged_scope"
SKIP_DUPLICATE = "duplicate_calendar_event"
SKIP_FAST_PATH = "fast_path"
SKIP_IMPOSSIBLE = "no_feasible_cleaner"
SKIP_SETTLED = "already_assigned"
SKIP_EMPTY = "no_open_work"


@dataclass
class TaskView:
    id: str
    property_id: str
    property_name: str
    property_timezone: str
    status: str
    window_start: datetime
    window_end: datetime
    duration_minutes: int
    assigned_cleaner_id: str | None
    assignment_id: str | None
    assignment_state: str | None
    scheduled_start: datetime | None
    scheduled_end: datetime | None
    started: bool


@dataclass
class WorkItem:
    action: str
    task_ids: list[str]
    reason: str
    evaluation: Evaluation | None = None
    cleaner: CleanerInput | None = None


@dataclass
class GateResult:
    items: list[WorkItem] = field(default_factory=list)
    fingerprint: str = ""


def _window(task: TaskView) -> JobWindow:
    return JobWindow(
        property_id=task.property_id,
        property_timezone=task.property_timezone,
        window_start=task.window_start,
        window_end=task.window_end,
        duration_minutes=task.duration_minutes,
    )


def _busy_for(task: TaskView, busy: list[BusyInterval]) -> list[BusyInterval]:
    return [item for item in busy if item.task_id != task.id]


def _stored_slot_fits(task: TaskView, cleaners: list[CleanerInput], busy: list[BusyInterval]) -> bool:
    """The saved times have to fit the current window. A cleaner who could start later is a change, not a preserve."""
    if task.scheduled_start is None or task.scheduled_end is None or not task.assigned_cleaner_id:
        return False
    from turnoverops.scheduling.windows import as_utc

    if as_utc(task.scheduled_start) < as_utc(task.window_start) or as_utc(task.scheduled_end) > as_utc(task.window_end):
        return False
    cleaner = next((item for item in cleaners if item.id == task.assigned_cleaner_id), None)
    if cleaner is None:
        return False
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(cleaner.timezone)
    local_start = as_utc(task.scheduled_start).astimezone(zone)
    local_end = as_utc(task.scheduled_end).astimezone(zone)
    if local_start.date() != local_end.date() or local_start.weekday() not in cleaner.working_days:
        return False
    if local_start.time() < cleaner.work_start or local_end.time() > cleaner.work_end:
        return False
    for item in busy:
        if item.cleaner_id != task.assigned_cleaner_id or item.task_id == task.id:
            continue
        if as_utc(task.scheduled_start) < as_utc(item.end) and as_utc(item.start) < as_utc(task.scheduled_end):
            return False
    return True


def assignment_feasible(task: TaskView, cleaners: list[CleanerInput], busy: list[BusyInterval]) -> bool:
    if not task.assigned_cleaner_id or task.assignment_state not in LIVE_ASSIGNMENT_STATES:
        return False
    if not _stored_slot_fits(task, cleaners, busy):
        return False
    decision = decide(cleaners, _window(task), _busy_for(task, busy))
    for item in (*decision.ranked_feasible, *decision.rejected):
        if item.cleaner_id == task.assigned_cleaner_id:
            return item.feasible
    return False


def feasible_ids(task: TaskView, cleaners: list[CleanerInput], busy: list[BusyInterval]) -> set[str]:
    decision = decide(cleaners, _window(task), _busy_for(task, busy))
    return {item.cleaner_id for item in decision.ranked_feasible}


def is_locked(task: TaskView) -> bool:
    return task.started or task.status in {"in_progress", "completed", "blocked"}


def needs_decision(task: TaskView, cleaners: list[CleanerInput], busy: list[BusyInterval]) -> bool:
    if task.status in {"completed", "blocked"} or is_locked(task):
        return False
    return not assignment_feasible(task, cleaners, busy)


def scope_fingerprint(tasks: list[TaskView], cleaners: list[CleanerInput]) -> str:
    payload = {
        "tasks": [
            {
                "id": task.id,
                "status": task.status,
                "window_start": task.window_start.isoformat(),
                "window_end": task.window_end.isoformat(),
                "duration": task.duration_minutes,
                "cleaner": task.assigned_cleaner_id,
                "state": task.assignment_state,
                "start": task.scheduled_start.isoformat() if task.scheduled_start else None,
                "end": task.scheduled_end.isoformat() if task.scheduled_end else None,
            }
            for task in sorted(tasks, key=lambda item: item.id)
        ],
        "cleaners": [
            {
                "id": cleaner.id,
                "active": cleaner.active,
                "days": sorted(cleaner.working_days),
                "start": cleaner.work_start.isoformat(),
                "end": cleaner.work_end.isoformat(),
                "max": cleaner.max_jobs_per_day,
                "eligible": sorted(cleaner.eligible_property_ids) if cleaner.eligible_property_ids is not None else None,
                "unavailable": sorted(item.isoformat() for item in cleaner.unavailable_dates),
            }
            for cleaner in sorted(cleaners, key=lambda item: item.id)
        ],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _competes(left: TaskView, right: TaskView, cleaners: list[CleanerInput], busy: list[BusyInterval]) -> bool:
    if is_locked(left) or is_locked(right):
        return False
    if not needs_decision(left, cleaners, busy) and not needs_decision(right, cleaners, busy):
        return False
    reduced = [item for item in busy if item.task_id not in {left.id, right.id}]
    # Keep feasible confirmed work as a fixed block by putting it back when that task is settled.
    for task in (left, right):
        if assignment_feasible(task, cleaners, busy) and task.scheduled_start and task.scheduled_end and task.assigned_cleaner_id:
            reduced.append(
                BusyInterval(task.assigned_cleaner_id, task.id, task.scheduled_start, task.scheduled_end)
            )
    return bool(feasible_ids(left, cleaners, reduced) & feasible_ids(right, cleaners, reduced))


def _components(tasks: list[TaskView], cleaners: list[CleanerInput], busy: list[BusyInterval]) -> list[list[TaskView]]:
    pending = [task for task in tasks if task.status not in {"completed", "blocked"}]
    parent = {task.id: task.id for task in pending}

    def find(task_id: str) -> str:
        while parent[task_id] != task_id:
            parent[task_id] = parent[parent[task_id]]
            task_id = parent[task_id]
        return task_id

    def union(left: str, right: str) -> None:
        parent[find(left)] = find(right)

    by_id = {task.id: task for task in pending}
    ids = list(by_id)
    for index, left_id in enumerate(ids):
        for right_id in ids[index + 1 :]:
            if _competes(by_id[left_id], by_id[right_id], cleaners, busy):
                union(left_id, right_id)
    grouped: dict[str, list[TaskView]] = {}
    for task in pending:
        grouped.setdefault(find(task.id), []).append(task)
    return list(grouped.values())


def _other_demands(task: TaskView, cleaner_id: str, tasks: list[TaskView], cleaners: list[CleanerInput], busy: list[BusyInterval]) -> bool:
    for other in tasks:
        if other.id == task.id or not needs_decision(other, cleaners, busy):
            continue
        reduced = [item for item in busy if item.task_id not in {task.id, other.id}]
        if cleaner_id in feasible_ids(other, cleaners, reduced):
            return True
    return False


def _movable(task: TaskView, tasks: list[TaskView], cleaners: list[CleanerInput], busy: list[BusyInterval]) -> bool:
    """Another assignment, if released, would make an eligible cleaner available."""
    if feasible_ids(task, cleaners, [item for item in busy if item.task_id != task.id]):
        return False
    for other in tasks:
        if other.id == task.id or not other.assigned_cleaner_id:
            continue
        if assignment_feasible(other, cleaners, busy):
            continue
        reduced = [item for item in busy if item.task_id not in {task.id, other.id}]
        if feasible_ids(task, cleaners, reduced):
            return True
    # A feasible sibling that shares a cleaner is movable competition, handled by components.
    return False


def expand_scope(
    tasks: list[TaskView],
    seed_ids: set[str] | None,
    cleaners: list[CleanerInput],
    busy: list[BusyInterval],
) -> list[TaskView]:
    """Daily and manual runs consider the horizon. Calendar runs start at the changed tasks."""
    open_tasks = [task for task in tasks if task.status not in {"completed", "blocked"}]
    if seed_ids is None:
        return open_tasks
    selected = {task.id for task in open_tasks if task.id in seed_ids}
    by_id = {task.id: task for task in open_tasks}
    changed = True
    while changed:
        changed = False
        for task in open_tasks:
            if task.id in selected:
                continue
            for chosen_id in list(selected):
                if _competes(task, by_id[chosen_id], cleaners, busy):
                    selected.add(task.id)
                    changed = True
                    break
    return [task for task in open_tasks if task.id in selected]


def gate(
    tasks: list[TaskView],
    cleaners: list[CleanerInput],
    busy: list[BusyInterval],
    *,
    allow_fast_path: bool,
) -> GateResult:
    fingerprint = scope_fingerprint(tasks, cleaners)
    open_tasks = [task for task in tasks if task.status not in {"completed", "blocked"}]
    if not open_tasks:
        return GateResult(
            items=[WorkItem("preserve", [], SKIP_EMPTY)],
            fingerprint=fingerprint,
        )
    items: list[WorkItem] = []
    settled = [task for task in open_tasks if not needs_decision(task, cleaners, busy)]
    if settled:
        items.append(WorkItem("preserve", [task.id for task in settled], SKIP_SETTLED))
    for component in _components([task for task in open_tasks if needs_decision(task, cleaners, busy)], cleaners, busy):
        if len(component) == 1 and allow_fast_path:
            task = component[0]
            decision = decide(cleaners, _window(task), _busy_for(task, busy))
            if (
                len(decision.ranked_feasible) == 1
                and not _other_demands(task, decision.selected.cleaner_id, open_tasks, cleaners, busy)
            ):
                chosen = decision.selected
                cleaner = next(item for item in cleaners if item.id == chosen.cleaner_id)
                items.append(WorkItem("fast_path", [task.id], SKIP_FAST_PATH, evaluation=chosen, cleaner=cleaner))
                continue
        impossible = []
        contested = []
        for task in component:
            structural = decide(cleaners, _window(task), [])
            if structural.selected is None and not _movable(task, open_tasks, cleaners, busy):
                impossible.append(task)
            else:
                contested.append(task)
        if impossible and not contested and len(component) == len(impossible):
            items.append(WorkItem("escalate", [task.id for task in impossible], SKIP_IMPOSSIBLE))
            continue
        if contested or len(component) > 1:
            items.append(WorkItem("agent", [task.id for task in component], "competing_tasks"))
            continue
        items.append(WorkItem("escalate", [task.id for task in component], SKIP_IMPOSSIBLE))
    return GateResult(items=items, fingerprint=fingerprint)
