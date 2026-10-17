"""Plain explanations for a property manager. Unknown causes stay general."""

from datetime import datetime
from zoneinfo import ZoneInfo

from turnoverops.scheduling.windows import as_utc

_EXACT = {
    "No feasible cleaner can take this turnover": "I checked the available cleaners, but I couldn't confirm a suitable assignment.",
    "Assignment retry limit reached": "I stopped after the assignment limit, so this cleaning needs your decision.",
    "Cleaning is in progress and can no longer be finished in the updated window": "Cleaning has already started, and there is no longer enough time to finish it.",
    "The assigned cleaner is no longer available": "The assigned cleaner can no longer take this job.",
    "The reservation changed while cleaning was in progress": "The reservation changed while cleaning was in progress, so the current plan no longer fits.",
    "The turnover was removed by a reservation change": "The reservation changed, so this cleaning was removed from the schedule.",
}


def plain_brief(summary: str) -> str:
    if summary in _EXACT:
        return _EXACT[summary]
    lowered = summary.lower()
    if "reservation" in lowered:
        return "The reservation changed, so the previous cleaning arrangement is no longer valid."
    if "declin" in lowered:
        return "A cleaner declined this job, so it needs a new assignment."
    if "cannot take this job" in lowered:
        return "The assigned cleaner can no longer take this job."
    if "in progress" in lowered:
        return "Cleaning has already started, and the current time no longer fits."
    return "This cleaning needs a new plan."


_FEED = {
    "Only http and https feeds are allowed": "Use an http or https calendar address.",
    "Feed URL must not include credentials": "Remove the username and password from the calendar address.",
    "Feed URL is missing a host": "That calendar address is missing a website name.",
    "Feed URL host is not allowed": "That calendar address is not allowed.",
    "Feed URL could not be resolved": "That calendar address could not be found.",
    "Feed URL did not resolve": "That calendar address could not be found.",
    "Feed URL resolves to a disallowed address": "That calendar address is not allowed.",
    "Feed request failed": "The calendar could not be read. Try again in a few minutes.",
    "Redirect is missing a location": "The calendar address redirected to an incomplete location.",
    "Feed response is too large": "The calendar file is too large to read.",
    "Too many redirects": "The calendar address redirected too many times.",
    "Calendar feed could not be parsed": "The calendar file could not be read.",
    "No calendar URL is stored": "This property has no calendar address.",
    "An iCal feed needs a URL": "Add a calendar address.",
    "Property has no reservation feed": "This property has no calendar feed.",
    "Unexpected sync failure": "The calendar could not be read. Try again in a few minutes.",
    "feed unavailable": "The calendar could not be read. Check the address and try again.",
}


def for_host(message: str) -> str:
    text = " ".join(message.split()).strip()
    if text in _EXACT:
        return _EXACT[text]
    if text in _FEED:
        return _FEED[text]
    lowered = text.lower()
    if lowered.startswith("feed request failed") or lowered.startswith("duplicate reservation"):
        return "The calendar could not be read. Check the address and try again."
    if any(token in lowered for token in ("traceback", "exception", "sqlalchemy", "errno")):
        return "Something went wrong. Try again in a few minutes."
    if lowered.startswith("feed ") or "ical" in lowered:
        return "The calendar could not be read. Check the address and try again."
    return text[:500]


def spoken_time(moment: datetime, timezone_name: str) -> str:
    local = as_utc(moment).astimezone(ZoneInfo(timezone_name))
    return f"{local.strftime('%A, %B')} {local.day} at {_clock(local)}"


def spoken_clock(moment: datetime, timezone_name: str) -> str:
    return _clock(as_utc(moment).astimezone(ZoneInfo(timezone_name)))


def _clock(local: datetime) -> str:
    hour = local.hour % 12 or 12
    suffix = "AM" if local.hour < 12 else "PM"
    return f"{hour}:{local.minute:02d} {suffix}"
