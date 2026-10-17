"""Gemini client for scheduling plans."""

from dataclasses import dataclass

import httpx

from turnoverops.core.config import Settings

SYSTEM_INSTRUCTION = (
    "You schedule short-term rental turnovers. Return only JSON matching the schema below. "
    "Do not include chain-of-thought, hidden reasoning, or markdown. "
    "reasoning_summary is one or two short operational sentences. "
    "Assign only task ids and cleaner ids present in the input. Never invent a cleaner. "
    "Put feasible confirmed and in-progress work in assignments_to_preserve. "
    "Put a task in assignments_to_change only when its current assignment cannot finish in the window. "
    "Use assignments_to_create for tasks with no live assignment. "
    "When nobody can take a task, add it to unassigned_tasks with reason_code, explanation, "
    "attempted_alternatives, and next_action. "
    "Every start, end, and deadline must include a timezone offset. "
    "Schema keys: planning_run_id, operational_date, timezone, assignments_to_create, "
    "assignments_to_change, assignments_to_preserve, unassigned_tasks, conflicts, warnings, "
    "reasoning_summary, requires_human_review. "
    "Each assignment object uses task_id, cleaner_id, start, end, deadline, explanation. "
    "Each preserve object uses task_id and explanation. "
    "Each unassigned object uses task_id, reason_code, explanation, attempted_alternatives, next_action. "
    "Each conflict object uses code, explanation, and optional task_id."
)


class AgentError(Exception):
    pass


@dataclass(frozen=True)
class AgentCompletion:
    text: str
    prompt_tokens: int | None = None
    output_tokens: int | None = None
    model: str = ""
    provider: str = "gemini"


class GeminiSchedulingClient:
    def complete(self, *, system: str, user: str, settings: Settings) -> AgentCompletion:
        if not settings.gemini_api_key:
            raise AgentError("GEMINI_API_KEY is not configured")
        model = settings.resolved_scheduling_model()
        response = httpx.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            params={"key": settings.gemini_api_key},
            json={
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {
                    "temperature": settings.scheduling_temperature,
                    "responseMimeType": "application/json",
                    "maxOutputTokens": settings.scheduling_max_output_tokens,
                },
            },
            timeout=30,
        )
        if response.status_code >= 300:
            raise AgentError(f"Scheduling model returned HTTP {response.status_code}")
        data = response.json()
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AgentError("Scheduling model returned no JSON text") from exc
        usage = data.get("usageMetadata") or {}
        prompt = usage.get("promptTokenCount")
        output = usage.get("candidatesTokenCount")
        return AgentCompletion(
            text=text,
            prompt_tokens=int(prompt) if prompt is not None else None,
            output_tokens=int(output) if output is not None else None,
            model=model,
            provider="gemini",
        )
