# TurnoverOps

Backend for a short-term rental host who needs cleaning tasks to follow reservation changes. The API proposes a cleaner and waits for that cleaner to accept. It does not mark the task confirmed before acceptance.

The scheduling rules are ordinary Python. Google sign-in identifies the host. Gemini, Telegram, and Google Calendar are optional: if they are not configured, task management still works. Properties can sync reservations from an iCalendar URL.

When a cleaner is added, TurnoverOps saves their details and sends a deduplicated welcome email through Gmail SMTP. The message includes an optional expiring Google Calendar connection link. Until they connect, scheduling uses configured working hours and labels external availability unverified. A connected calendar that cannot be checked is excluded from automatic scheduling.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
alembic upgrade head
uvicorn turnoverops.main:app --reload
```

## Render deployment

The repository includes a `Dockerfile` and `render.yaml` for a single Render web service. Set `DATABASE_URL` to the Neon PostgreSQL connection string using the `postgresql+psycopg://` scheme, then deploy with the Render Blueprint. Render runs `alembic upgrade head` once as its pre-deploy command, then starts Uvicorn from the image. The service exposes `/health` for Render health checks.

Keep `ENABLE_SCHEDULER=true` with one running web instance. The in-process scheduler handles calendar polling and daily planning; do not run a second external scheduler against the same database unless the application is changed to use a protected maintenance endpoint and scheduler locking.

Set `GOOGLE_CLIENT_ID` to the web client ID used by the frontend. The React app sends the Google ID token to `POST /auth/google`. The API checks it and returns its own bearer token. Send that token as `Authorization: Bearer ...`.

For cleaner Calendar availability, configure one Google OAuth web client using `GOOGLE_CALENDAR_CLIENT_ID` and `GOOGLE_CALENDAR_CLIENT_SECRET`. Add both redirect URIs to that client's authorized redirect URI list: the existing `GOOGLE_CALENDAR_REDIRECT_URI` and `CLEANER_GOOGLE_CALENDAR_REDIRECT_URI` (the backend callback). Enable the Google Calendar API and configure the OAuth consent screen. Cleaners are asked only for the `calendar.freebusy` scope. Set `APP_BASE_URL` to the publicly reachable backend URL so welcome links reach the API. Configure `SMTP_USERNAME`, `SMTP_PASSWORD` (a Gmail App Password), and `EMAIL_FROM` to deliver the welcome email. Run `alembic upgrade head` after deploying the schema change.

OpenAPI is at `/docs`.

## Calendar sync

Add a property's public iCalendar URL to sync its reservations. Local, private, and redirected private addresses are rejected. If a fetch fails, existing reservations and tasks stay as they were, and the property records the sync error.

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
| `GOOGLE_CALENDAR_REDIRECT_URI` | Existing host calendar OAuth callback in the frontend |
| `CLEANER_GOOGLE_CALENDAR_REDIRECT_URI` | Backend callback for cleaner availability consent |
| `APP_BASE_URL` | Public backend base URL used in cleaner connection links |
| `SMTP_USERNAME` / `SMTP_PASSWORD` / `EMAIL_FROM` | Gmail SMTP sender for cleaner welcome emails |

## Tests

```bash
pytest
```

External services are mocked. The suite covers duplicate imports, infeasible cleaners, confirmation, bounded decline, escalation, Telegram deduplication, invite expiry, ambiguous intent, notification retry, host isolation, and a calendar failure that leaves the confirmed task in place.
