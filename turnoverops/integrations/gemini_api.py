import json
import re
from dataclasses import dataclass

import httpx

from turnoverops.core.config import Settings
from turnoverops.domain import INTENTS

EXACT_COMMANDS = {
    "accept": "ACCEPT_ASSIGNMENT",
    "yes": "ACCEPT_ASSIGNMENT",
    "decline": "DECLINE_ASSIGNMENT",
    "no": "DECLINE_ASSIGNMENT",
    "start": "START_CLEANING",
    "complete": "COMPLETE_CLEANING",
    "done": "COMPLETE_CLEANING",
    "delay": "REPORT_DELAY",
    "unavailable": "REPORT_UNAVAILABILITY",
}

CLARIFY = "Reply with accept, decline, start, complete, delay, or unavailable."


@dataclass(frozen=True)
class InterpretedIntent:
    intent: str
    confidence: float
    clarification: str | None
    source: str


def local_interpret(text: str) -> InterpretedIntent:
    token = re.sub(r"[^a-z\s]", "", text.strip().lower())
    token = " ".join(token.split())
    intent = EXACT_COMMANDS.get(token)
    if intent is None:
        return InterpretedIntent("UNKNOWN", 0.0, CLARIFY, "local")
    return InterpretedIntent(intent, 1.0, None, "local")


def _prompt(text: str) -> str:
    allowed = ", ".join(sorted(INTENTS))
    return (
        "Classify the cleaner message into one intent. "
        "Do not invent times, availability, or task outcomes. "
        f"Allowed intents: {allowed}. "
        "If the message is ambiguous, use UNKNOWN and write a short clarification. "
        'Return JSON: {"intent": "...", "confidence": 0.0, "clarification": null}. '
        f"Message: {text}"
    )


def _from_payload(payload: dict, source: str) -> InterpretedIntent:
    intent = str(payload.get("intent") or "UNKNOWN")
    if intent not in INTENTS:
        intent = "UNKNOWN"
    try:
        confidence = float(payload.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0
    clarification = payload.get("clarification")
    if intent == "UNKNOWN" and not clarification:
        clarification = CLARIFY
    return InterpretedIntent(intent, confidence, clarification, source)


def interpret(text: str, settings: Settings, *, client: httpx.Client | None = None) -> InterpretedIntent:
    if not settings.gemini_api_key:
        return local_interpret(text)
    owns_client = client is None
    http = client or httpx.Client()
    try:
        response = http.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{settings.gemini_model}:generateContent",
            params={"key": settings.gemini_api_key},
            json={
                "contents": [{"role": "user", "parts": [{"text": _prompt(text)}]}],
                "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
            },
            timeout=8,
        )
        response.raise_for_status()
        data = response.json()
        raw = data["candidates"][0]["content"]["parts"][0]["text"]
        return _from_payload(json.loads(raw), "gemini")
    except (httpx.HTTPError, KeyError, IndexError, TypeError, json.JSONDecodeError):
        return InterpretedIntent("UNKNOWN", 0.0, "The assistant is unavailable. " + CLARIFY, "unavailable")
    finally:
        if owns_client:
            http.close()
