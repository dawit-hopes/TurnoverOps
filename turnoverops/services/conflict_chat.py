"""Task-scoped natural-language conflict help backed by deterministic scheduling."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from turnoverops.core.config import Settings
from turnoverops.core.errors import AppError
from turnoverops.domain import LIVE_ASSIGNMENT_STATES, LOCKED_TASK_STATUSES, AssignmentState, TaskStatus
from turnoverops.integrations.scheduling_agent import AgentError, interpret_conflict_instruction
from turnoverops.models import Assignment, Cleaner, CleaningTask, Property
from turnoverops.scheduling.engine import Decision, Evaluation
from turnoverops.services.evaluation import evaluate_window, task_window
from turnoverops.services.host_policy import load_policy
from turnoverops.services.workflow import reassign_task

_CONFIRMATION_TTL_SECONDS = 5 * 60
_REASON_COPY = {
    "inactive": "is inactive",
    "calendar_unavailable": "their Google Calendar could not be checked",
    "unavailable": "is marked unavailable on that date",
    "not_eligible": "is not eligible for this property",
    "insufficient_window": "the cleaning window is shorter than the required duration",
    "overlap": "has another job during the available window",
    "outside_working_hours": "the available time is outside working hours",
    "daily_job_limit": "has reached the daily job limit",
}


def handle_conflict_message(
    db: Session,
    task: CleaningTask,
    prop: Property,
    host_id: str,
    message: str,
    settings: Settings,
    now: datetime,
    *,
    confirmation_token: str | None = None,
) -> dict:
    if confirmation_token:
        return _confirm_reassignment(db, task, prop, host_id, confirmation_token, settings, now)

    cleaners = db.scalars(select(Cleaner).where(Cleaner.host_id == host_id, Cleaner.active.is_(True))).all()
    try:
        instruction = interpret_conflict_instruction(message, [cleaner.name for cleaner in cleaners], settings)
    except Exception as exc:
        if isinstance(exc, AgentError):
            detail = str(exc)
        else:
            detail = "The assistant is unavailable. Please try again later."
        raise AppError(503, "assistant_unavailable", detail) from exc

    if instruction.action == "clarify":
        return {"reply": "Tell me whether you want me to assign a specific cleaner, find another cleaner, retry, or explain the conflict.", "action": "message"}

    decision = evaluate_window(
        db,
        host_id,
        task_window(task, prop.timezone),
        exclude_task_id=task.id,
        settings=settings,
    )
    reason = str((task.conflict_details or {}).get("summary") or "No validated cleaner is currently assigned.")
    if instruction.action == "explain":
        return {"reply": _explain_conflict(reason, decision), "action": "message"}

    if task.started_at is not None or task.status in LOCKED_TASK_STATUSES or task.status in {TaskStatus.completed, TaskStatus.blocked}:
        return {"reply": "This cleaning can no longer be reassigned automatically because work has started or the task is closed.", "action": "message"}

    selected: Evaluation | None = None
    if instruction.action == "assign_cleaner":
        wanted = (instruction.cleaner_name or "").strip()
        matches = [cleaner for cleaner in cleaners if cleaner.name.casefold() == wanted.casefold()]
        if len(matches) != 1:
            return {"reply": f"I couldn't match “{wanted or 'that cleaner'}” to one active cleaner on this account. Check the cleaner's name and try again.", "action": "message"}
        cleaner = matches[0]
        selected = next((item for item in decision.ranked_feasible if item.cleaner_id == cleaner.id), None)
        if selected is None:
            rejected = next((item for item in decision.rejected if item.cleaner_id == cleaner.id), None)
            return {"reply": _infeasible_reply(cleaner.name, rejected), "action": "message"}
    else:
        ranked = list(decision.ranked_feasible)
        current = db.get(Assignment, task.current_assignment_id) if task.current_assignment_id else None
        if current is not None and current.state in LIVE_ASSIGNMENT_STATES and task.status != TaskStatus.at_risk:
            cleaner = db.get(Cleaner, current.cleaner_id)
            name = cleaner.name if cleaner is not None else "A cleaner"
            return {"reply": f"{name} is already assigned and the task is not marked as needing a new plan. I left the assignment unchanged.", "action": "message"}
        if current is not None and current.state in LIVE_ASSIGNMENT_STATES:
            ranked = [item for item in ranked if item.cleaner_id != current.cleaner_id]
        selected = ranked[0] if ranked else None
        if selected is None:
            return {"reply": _no_available_reply(decision, current.cleaner_id if current else None), "action": "message"}

    current = db.get(Assignment, task.current_assignment_id) if task.current_assignment_id else None
    if current is not None and current.state in LIVE_ASSIGNMENT_STATES and current.cleaner_id == selected.cleaner_id:
        cleaner = db.get(Cleaner, selected.cleaner_id)
        name = cleaner.name if cleaner is not None else selected.cleaner_name
        return {"reply": f"{name} is already assigned. I left the assignment unchanged.", "action": "assigned", "cleaner_id": selected.cleaner_id, "cleaner_name": name}

    failures = db.scalars(
        select(Assignment).where(
            Assignment.task_id == task.id,
            Assignment.state.in_((AssignmentState.declined, AssignmentState.expired)),
        )
    ).all()
    attempt_limit_reached = sum(
        1 for row in failures if row.window_start == task.window_start and row.window_end == task.window_end
    ) >= settings.max_assignment_attempts

    if task.current_assignment_id:
        if current is not None and current.state in LIVE_ASSIGNMENT_STATES and current.cleaner_id != selected.cleaner_id:
            cleaner = db.get(Cleaner, selected.cleaner_id)
            name = cleaner.name if cleaner is not None else selected.cleaner_name
            token = _confirmation_token(host_id, task, selected.cleaner_id, settings, now)
            return {
                "reply": f"{name} is feasible under the current availability and scheduling rules. Replacing the current assignment is a significant change. Confirm to continue.",
                "action": "proposed",
                "confirmation_token": token,
                "cleaner_id": selected.cleaner_id,
                "cleaner_name": name,
            }

    if attempt_limit_reached:
        token = _confirmation_token(host_id, task, selected.cleaner_id, settings, now)
        return {
            "reply": f"{selected.cleaner_name} is feasible, but the automatic retry limit has been reached. Confirm to proceed with this manual assignment.",
            "action": "proposed",
            "confirmation_token": token,
            "cleaner_id": selected.cleaner_id,
            "cleaner_name": selected.cleaner_name,
        }

    return _apply_assignment(db, task, prop, host_id, selected.cleaner_id, settings, now)


def _confirmation_token(host_id: str, task: CleaningTask, cleaner_id: str, settings: Settings, now: datetime) -> str:
    payload = {
        "host_id": host_id,
        "task_id": task.id,
        "cleaner_id": cleaner_id,
        "previous_assignment_id": task.current_assignment_id,
        "assignment_version": task.assignment_version,
        "expires_at": int((now + timedelta(seconds=_CONFIRMATION_TTL_SECONDS)).timestamp()),
    }
    raw = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    signature = hmac.new(settings.jwt_secret.encode(), raw.encode(), hashlib.sha256).digest()
    signed = base64.urlsafe_b64encode(signature).decode().rstrip("=")
    return f"{raw}.{signed}"


def _read_confirmation_token(token: str, settings: Settings, now: datetime) -> dict:
    try:
        raw, signed = token.split(".", 1)
        expected = hmac.new(settings.jwt_secret.encode(), raw.encode(), hashlib.sha256).digest()
        supplied = base64.urlsafe_b64decode(signed + "=" * (-len(signed) % 4))
        if not hmac.compare_digest(expected, supplied):
            raise ValueError
        payload = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        if not isinstance(payload, dict) or int(payload.get("expires_at", 0)) <= int(now.timestamp()):
            raise ValueError
        return payload
    except (ValueError, TypeError, json.JSONDecodeError, binascii.Error) as exc:
        raise AppError(400, "invalid_confirmation", "This confirmation expired or is invalid. Send a new instruction.") from exc


def _confirm_reassignment(
    db: Session,
    task: CleaningTask,
    prop: Property,
    host_id: str,
    token: str,
    settings: Settings,
    now: datetime,
) -> dict:
    payload = _read_confirmation_token(token, settings, now)
    if payload.get("host_id") != host_id or payload.get("task_id") != task.id:
        raise AppError(400, "invalid_confirmation", "This confirmation does not apply to this cleaning.")
    cleaner_id = payload.get("cleaner_id")
    if not isinstance(cleaner_id, str):
        raise AppError(400, "invalid_confirmation", "This confirmation is invalid.")
    if task.current_assignment_id != payload.get("previous_assignment_id"):
        current = db.get(Assignment, task.current_assignment_id) if task.current_assignment_id else None
        if current is not None and current.cleaner_id == cleaner_id and current.state in LIVE_ASSIGNMENT_STATES:
            cleaner = db.get(Cleaner, cleaner_id)
            return {"reply": f"{cleaner.name if cleaner else 'The cleaner'} is already assigned. No duplicate assignment was created.", "action": "assigned", "cleaner_id": cleaner_id, "cleaner_name": cleaner.name if cleaner else None}
        raise AppError(409, "assignment_changed", "The assignment changed while you were reviewing it. Check the latest task status and try again.")
    if task.assignment_version != payload.get("assignment_version"):
        raise AppError(409, "assignment_changed", "The cleaning changed while you were reviewing it. Check the latest task status and try again.")
    return _apply_assignment(db, task, prop, host_id, cleaner_id, settings, now)


def _apply_assignment(db: Session, task: CleaningTask, prop: Property, host_id: str, cleaner_id: str, settings: Settings, now: datetime) -> dict:
    try:
        reassign_task(db, task, prop, now, settings, cleaner_id=cleaner_id, policy=load_policy(db, host_id))
    except AppError as exc:
        if exc.code == "infeasible":
            return {"reply": "The schedule changed or this cleaner is no longer feasible. No assignment was made. Refresh the conflict and try again.", "action": "message"}
        raise
    db.refresh(task)
    assignment = db.get(Assignment, task.current_assignment_id) if task.current_assignment_id else None
    if assignment is None or assignment.cleaner_id != cleaner_id or assignment.state not in LIVE_ASSIGNMENT_STATES:
        raise AppError(409, "assignment_not_saved", "The assignment was not confirmed in the database. Refresh the task before trying again.")
    cleaner = db.get(Cleaner, cleaner_id)
    name = cleaner.name if cleaner is not None else "The cleaner"
    return {"reply": f"Assigned to {name}. The schedule was validated and saved.", "action": "assigned", "cleaner_id": cleaner_id, "cleaner_name": name}


def _explain_conflict(reason: str, decision: Decision) -> str:
    if decision.ranked_feasible:
        names = ", ".join(item.cleaner_name for item in decision.ranked_feasible[:3])
        return f"The recorded conflict is: {reason} Current scheduling checks show {names} can meet the deadline. No assignment was changed."
    return _no_available_reply(decision)


def _no_available_reply(decision: Decision, excluded_cleaner_id: str | None = None) -> str:
    if not decision.rejected:
        return "No active cleaners are available to evaluate for this cleaning. Add an eligible cleaner or adjust the schedule."
    details = []
    for item in decision.rejected:
        if item.cleaner_id == excluded_cleaner_id:
            continue
        causes = [_REASON_COPY.get(reason, reason.replace("_", " ")) for reason in item.reasons]
        details.append(f"{item.cleaner_name}: {', '.join(causes) or 'not feasible'}")
    if not details:
        return "No other cleaner is currently available for this cleaning. The current assignment was left unchanged."
    return "No eligible cleaner can currently take this cleaning. " + "; ".join(details) + ". No assignment was changed."


def _infeasible_reply(name: str, evaluation: Evaluation | None) -> str:
    if evaluation is None:
        return f"{name} is not feasible for this cleaning under the current scheduling rules. No assignment was changed."
    causes = [_REASON_COPY.get(reason, reason.replace("_", " ")) for reason in evaluation.reasons]
    return f"I can't assign {name}: {', '.join(causes)}. No assignment was changed."
