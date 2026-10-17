from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from turnoverops.core.config import Settings
from turnoverops.core.errors import AppError
from turnoverops.domain import INTENT_CONFIDENCE, AssignmentState, TaskStatus
from turnoverops.integrations.gemini_api import InterpretedIntent
from turnoverops.integrations import gemini_api
from turnoverops.models import Assignment, Cleaner, CleaningTask, ProcessedUpdate, Property
from turnoverops.services.invites import connect_telegram
from turnoverops.services.workflow import (
    accept_assignment,
    close_assignment,
    complete_cleaning,
    report_delay,
    report_unavailability,
    start_cleaning,
)

CALLBACKS = {
    "accept": "ACCEPT_ASSIGNMENT",
    "decline": "DECLINE_ASSIGNMENT",
    "start": "START_CLEANING",
    "complete": "COMPLETE_CLEANING",
    "delay": "REPORT_DELAY",
    "unavailable": "REPORT_UNAVAILABILITY",
}


def handle_update(db: Session, update: dict, settings: Settings, now: datetime | None = None) -> dict:
    moment = now or datetime.now(UTC)
    update_id = update.get("update_id")
    if update_id is None:
        raise AppError(422, "invalid_update", "update_id is required")
    key = str(update_id)
    existing = db.scalar(
        select(ProcessedUpdate).where(ProcessedUpdate.provider == "telegram", ProcessedUpdate.update_id == key)
    )
    if existing is not None:
        return {"status": "duplicate"}
    db.add(ProcessedUpdate(provider="telegram", update_id=key))
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return {"status": "duplicate"}
    message, callback = _parts(update)
    if callback:
        user = callback.get("from") or {}
        chat = (callback.get("message") or {}).get("chat") or {}
        text = ""
        data = str(callback.get("data") or "")
    else:
        user = message.get("from") or {}
        chat = message.get("chat") or {}
        text = str(message.get("text") or "")
        data = ""
    user_id = user.get("id")
    chat_id = chat.get("id")
    if user_id is None or chat_id is None:
        return {"status": "ignored"}
    if text.startswith("/start"):
        token = text.split(maxsplit=1)[1].strip() if " " in text else ""
        if not token:
            return {"status": "clarify", "clarification": "Send the connection link you were given."}
        cleaner = connect_telegram(db, token, str(user_id), str(chat_id), moment)
        return {"status": "connected", "cleaner_id": cleaner.id}
    cleaner = db.scalar(select(Cleaner).where(Cleaner.telegram_user_id == str(user_id), Cleaner.active.is_(True)))
    if cleaner is None:
        return {"status": "clarify", "clarification": "Connect with your invite link before sending commands."}
    intent, assignment_id = _intent(text, data, settings)
    if intent.intent == "UNKNOWN" or intent.confidence < INTENT_CONFIDENCE:
        return {"status": "clarify", "clarification": intent.clarification or "Please reply with a clearer command."}
    assignment, task = _select_assignment(db, cleaner, intent.intent, assignment_id)
    prop = db.get(Property, task.property_id)
    if prop is None:
        raise AppError(404, "not_found", "Not found")
    _apply(db, intent.intent, assignment, task, prop, moment, settings)
    return {"status": "applied", "intent": intent.intent, "task_id": task.id}


def _parts(update: dict) -> tuple[dict, dict | None]:
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        return {}, callback
    message = update.get("message")
    return message if isinstance(message, dict) else {}, None


def _intent(text: str, data: str, settings: Settings) -> tuple[InterpretedIntent, str | None]:
    if data:
        action, _, assignment_id = data.partition(":")
        mapped = CALLBACKS.get(action)
        if mapped:
            return InterpretedIntent(mapped, 1.0, None, "callback"), assignment_id or None
    return gemini_api.interpret(text, settings), None


def _select_assignment(db: Session, cleaner: Cleaner, intent: str, assignment_id: str | None) -> tuple[Assignment, CleaningTask]:
    if assignment_id:
        assignment = db.get(Assignment, assignment_id)
        task = db.get(CleaningTask, assignment.task_id) if assignment else None
        if assignment is None or task is None or assignment.cleaner_id != cleaner.id or task.host_id != cleaner.host_id:
            raise AppError(404, "not_found", "Not found")
        return assignment, task
    stmt = (
        select(Assignment, CleaningTask)
        .join(CleaningTask, CleaningTask.id == Assignment.task_id)
        .where(
            Assignment.cleaner_id == cleaner.id,
            Assignment.id == CleaningTask.current_assignment_id,
            CleaningTask.host_id == cleaner.host_id,
        )
    )
    if intent in {"ACCEPT_ASSIGNMENT", "DECLINE_ASSIGNMENT"}:
        stmt = stmt.where(
            Assignment.state == AssignmentState.awaiting_confirmation,
            CleaningTask.status == TaskStatus.awaiting_confirmation,
        )
    elif intent == "START_CLEANING":
        stmt = stmt.where(Assignment.state == AssignmentState.accepted, CleaningTask.status == TaskStatus.confirmed)
    elif intent in {"COMPLETE_CLEANING", "REPORT_DELAY"}:
        stmt = stmt.where(
            Assignment.state == AssignmentState.accepted,
            CleaningTask.status.in_((TaskStatus.confirmed, TaskStatus.in_progress)),
        )
    else:
        stmt = stmt.where(
            CleaningTask.status.in_((TaskStatus.awaiting_confirmation, TaskStatus.confirmed, TaskStatus.in_progress))
        )
    rows = db.execute(stmt).all()
    if len(rows) != 1:
        raise AppError(409, "ambiguous", "Which assignment do you mean? Reply about one task.")
    return rows[0]


def _apply(db, intent, assignment, task, prop, now, settings) -> None:
    if intent == "ACCEPT_ASSIGNMENT":
        accept_assignment(db, assignment, now, settings, actor="cleaner")
    elif intent == "DECLINE_ASSIGNMENT":
        close_assignment(
            db,
            assignment,
            now,
            settings,
            state=AssignmentState.declined,
            actor="cleaner",
            reason="Cleaner declined the assignment",
        )
    elif intent == "START_CLEANING":
        start_cleaning(db, task, now, actor="cleaner")
    elif intent == "COMPLETE_CLEANING":
        complete_cleaning(db, task, prop, now, settings, actor="cleaner")
    elif intent == "REPORT_DELAY":
        report_delay(db, task, prop, actor="cleaner")
    elif intent == "REPORT_UNAVAILABILITY":
        report_unavailability(db, task, prop, now, settings, actor="cleaner")
    else:
        raise AppError(409, "ambiguous", "That message does not map to a task action.")
