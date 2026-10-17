from urllib.parse import quote, urlencode

import httpx

from turnoverops.core.config import Settings

CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"


class CalendarError(Exception):
    pass


def authorization_url(settings: Settings, state: str) -> str:
    query = urlencode(
        {
            "client_id": settings.google_calendar_client_id,
            "redirect_uri": settings.google_calendar_redirect_uri,
            "response_type": "code",
            "scope": CALENDAR_SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
    )
    return f"https://accounts.google.com/o/oauth2/v2/auth?{query}"


def exchange_code(settings: Settings, code: str, *, client: httpx.Client | None = None) -> dict:
    return _token_request(
        {
            "code": code,
            "client_id": settings.google_calendar_client_id,
            "client_secret": settings.google_calendar_client_secret,
            "redirect_uri": settings.google_calendar_redirect_uri,
            "grant_type": "authorization_code",
        },
        client=client,
    )


def refresh_access_token(settings: Settings, refresh_token: str, *, client: httpx.Client | None = None) -> dict:
    return _token_request(
        {
            "refresh_token": refresh_token,
            "client_id": settings.google_calendar_client_id,
            "client_secret": settings.google_calendar_client_secret,
            "grant_type": "refresh_token",
        },
        client=client,
    )


def _token_request(data: dict, *, client: httpx.Client | None) -> dict:
    owns_client = client is None
    http = client or httpx.Client()
    try:
        try:
            response = http.post("https://oauth2.googleapis.com/token", data=data, timeout=10)
        except httpx.HTTPError as exc:
            raise CalendarError("Google authorization request failed") from exc
        if response.status_code >= 400:
            raise CalendarError("Google authorization was rejected")
        payload = response.json()
        if not payload.get("access_token"):
            raise CalendarError("Google did not return an access token")
        return payload
    finally:
        if owns_client:
            http.close()


def upsert_event(
    access_token: str,
    calendar_id: str,
    event_id: str | None,
    body: dict,
    *,
    client: httpx.Client | None = None,
) -> str:
    owns_client = client is None
    http = client or httpx.Client()
    calendar = quote(calendar_id, safe="")
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        try:
            if event_id:
                url = f"https://www.googleapis.com/calendar/v3/calendars/{calendar}/events/{quote(event_id, safe='')}"
                response = http.put(url, headers=headers, json=body, timeout=10)
            else:
                url = f"https://www.googleapis.com/calendar/v3/calendars/{calendar}/events"
                response = http.post(url, headers=headers, json=body, timeout=10)
        except httpx.HTTPError as exc:
            raise CalendarError("Google Calendar request failed") from exc
        if response.status_code >= 400:
            raise CalendarError(f"Google Calendar rejected the event ({response.status_code})")
        event = response.json().get("id")
        if not event:
            raise CalendarError("Google Calendar did not return an event id")
        return str(event)
    finally:
        if owns_client:
            http.close()
