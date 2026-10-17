from enum import StrEnum


class TaskStatus(StrEnum):
    unassigned = "unassigned"
    awaiting_confirmation = "awaiting_confirmation"
    confirmed = "confirmed"
    in_progress = "in_progress"
    completed = "completed"
    at_risk = "at_risk"
    blocked = "blocked"


class AssignmentState(StrEnum):
    proposed = "proposed"
    awaiting_confirmation = "awaiting_confirmation"
    accepted = "accepted"
    declined = "declined"
    expired = "expired"
    cancelled = "cancelled"


class FeedKind(StrEnum):
    none = "none"
    demo = "demo"
    ical = "ical"


class SyncStatus(StrEnum):
    never = "never"
    ok = "ok"
    error = "error"


OPEN_TASK_STATUSES = {
    TaskStatus.unassigned,
    TaskStatus.awaiting_confirmation,
    TaskStatus.confirmed,
    TaskStatus.in_progress,
    TaskStatus.at_risk,
}

BLOCKING_TASK_STATUSES = {
    TaskStatus.awaiting_confirmation,
    TaskStatus.confirmed,
    TaskStatus.in_progress,
}

LOCKED_TASK_STATUSES = {TaskStatus.in_progress, TaskStatus.completed}

TERMINAL_ASSIGNMENT_STATES = {
    AssignmentState.declined,
    AssignmentState.expired,
    AssignmentState.cancelled,
}

LIVE_ASSIGNMENT_STATES = {
    AssignmentState.awaiting_confirmation,
    AssignmentState.accepted,
}

INTENTS = {
    "ACCEPT_ASSIGNMENT",
    "DECLINE_ASSIGNMENT",
    "REPORT_DELAY",
    "START_CLEANING",
    "COMPLETE_CLEANING",
    "REPORT_UNAVAILABILITY",
    "UNKNOWN",
}

INTENT_CONFIDENCE = 0.7
