"""JSON contract for the scheduling agent. Requests that do not match are rejected."""

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from turnoverops.scheduling.windows import parse_aware


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _aware(value: datetime) -> datetime:
    return parse_aware(value)


class AssignmentProposal(_Strict):
    task_id: str = Field(min_length=1)
    cleaner_id: str = Field(min_length=1)
    start: datetime
    end: datetime
    deadline: datetime
    explanation: str = Field(min_length=1, max_length=500)

    @field_validator("start", "end", "deadline")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        return _aware(value)


class PreserveProposal(_Strict):
    task_id: str = Field(min_length=1)
    explanation: str = Field(default="", max_length=500)


class UnassignedProposal(_Strict):
    task_id: str = Field(min_length=1)
    reason_code: str = Field(min_length=1, max_length=64)
    explanation: str = Field(min_length=1, max_length=500)
    attempted_alternatives: list[str]
    next_action: str = Field(min_length=1, max_length=300)


class ConflictProposal(_Strict):
    code: str = Field(min_length=1, max_length=64)
    explanation: str = Field(min_length=1, max_length=500)
    task_id: str | None = None


class SchedulePlan(_Strict):
    planning_run_id: str = Field(min_length=1)
    operational_date: date
    timezone: str = Field(min_length=1)
    assignments_to_create: list[AssignmentProposal]
    assignments_to_change: list[AssignmentProposal]
    assignments_to_preserve: list[PreserveProposal]
    unassigned_tasks: list[UnassignedProposal]
    conflicts: list[ConflictProposal]
    warnings: list[str]
    reasoning_summary: str = Field(max_length=500)
    requires_human_review: bool


class PlanParseError(ValueError):
    pass


def parse_plan(text: str) -> SchedulePlan:
    import json

    from pydantic import ValidationError

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PlanParseError("Agent response was not valid JSON") from exc
    try:
        return SchedulePlan.model_validate(payload)
    except ValidationError as exc:
        raise PlanParseError("Agent response did not match the planning schema") from exc
