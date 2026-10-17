# TurnoverOps

Backend for a short-term rental host who needs cleaning tasks to follow reservation changes. The API proposes a cleaner and waits for that cleaner to accept. It does not mark the task confirmed before acceptance.

The scheduling rules are ordinary Python. Google sign-in identifies the host. Gemini, Telegram, and Google Calendar are optional: if they are not configured, task management still works. Demo reservations can be imported as JSON, so a walkthrough does not need a live calendar feed. An iCalendar URL is supported when a property actually has one.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
alembic upgrade head
uvicorn turnoverops.main:app --reload
```

Set `GOOGLE_CLIENT_ID` to the web client ID used by the frontend. The React app sends the Google ID token to `POST /auth/google`. The API checks it and returns its own bearer token. Send that token as `Authorization: Bearer ...`.

OpenAPI is at `/docs`.

## Reservation import

`POST /properties/{id}/sync` accepts a demo payload:

```json
{
  "reservations": [
    {
      "external_uid": "stay-1",
      "start": "2026-11-01T15:00:00Z",
      "end": "2026-11-03T10:00:00Z"
    }
  ]
}
```

The same shape is in [examples/demo_feed.json](examples/demo_feed.json). Syncing the same payload again updates rows in place. A reservation that disappears from the payload is marked cancelled. The response includes `feed_kind`, `sync_status`, `last_synced_at`, and `last_sync_error`. Demo data is stored as `feed_kind: "demo"` and is not described as a live feed.

To use an iCalendar feed instead, set `feed_kind` to `ical` and `ical_url` to a public `http` or `https` URL. Local, private, and redirected private addresses are rejected. If the fetch fails, existing reservations and tasks stay as they were and the property records the error.

## Assignment flow

Imported stays become cleaning tasks for the gap between checkout and the next check-in. The scheduler keeps a cleaner only when they are active, allowed on the property, inside working hours, free of other active jobs, and able to finish before the next check-in.

A feasible cleaner is stored as `awaiting_confirmation`. `POST /assignments/{id}/accept` moves the task to `confirmed`. A decline, or a confirmation timeout, tries the next feasible cleaner until `MAX_ASSIGNMENT_ATTEMPTS`. After that the task becomes `at_risk` and the host is notified. Work that has already started is not given to someone else; the host is asked to intervene.

`DEV_MODE=true` exposes `POST /dev/telegram/updates` for a Telegram update without a bot. Those responses include `"simulated": true`. Cleaner messages that are not an exact command stay unanswered until they are clear, and they do not change the task.

## Environment

See [.env.example](.env.example). Secrets stay in the environment. `FRONTEND_ORIGIN` is the only browser origin allowed by CORS.

| Variable | Purpose |
| --- | --- |
| `GOOGLE_CLIENT_ID` | Audience for host sign-in |
| `JWT_SECRET` | Signs the API session token |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_WEBHOOK_SECRET` | Cleaner messages. Webhooks are rejected without the secret |
| `GEMINI_API_KEY` | Optional intent parsing. Exact commands still work without it |
| `GOOGLE_CALENDAR_CLIENT_ID` / `GOOGLE_CALENDAR_CLIENT_SECRET` | Optional calendar publishing after a cleaner accepts |

## Tests

```bash
pytest
```

External services are mocked. The suite covers duplicate imports, infeasible cleaners, confirmation, bounded decline, escalation, Telegram deduplication, invite expiry, ambiguous intent, notification retry, host isolation, and a calendar failure that leaves the confirmed task in place.
