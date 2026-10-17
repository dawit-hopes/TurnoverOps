"""Public cleaner links and rejection pages."""

from datetime import UTC, datetime

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from turnoverops.api.deps import AppSettings, Db
from turnoverops.services.cleaner_calendar import authorization_url, calendar_configured, finish_authorization
from turnoverops.services.cleaner_mail import rejection_confirm, rejection_details, rejection_preview

router = APIRouter(tags=["rejection"])


def _calendar_result_page(message: str, *, success: bool) -> str:
    icon = "✓" if success else "!"
    color = "#16845b" if success else "#b54708"
    background = "#e8f5ef" if success else "#fff3e5"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#f5f7fb">
  <title>Google Calendar · TurnoverOps</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      padding: 24px;
      background: #f5f7fb;
      color: #172033;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    main {{
      width: min(100%, 480px);
      padding: 40px 32px;
      border: 1px solid #e5e9f0;
      border-radius: 20px;
      background: #fff;
      box-shadow: 0 18px 55px rgba(23, 32, 51, .08);
      text-align: center;
    }}
    .brand {{ margin: 0 0 28px; color: #596579; font-size: 14px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; }}
    .icon {{
      display: grid;
      width: 64px;
      height: 64px;
      margin: 0 auto 22px;
      place-items: center;
      border-radius: 50%;
      background: {background};
      color: {color};
      font-size: 34px;
      font-weight: 700;
    }}
    h1 {{ margin: 0; font-size: clamp(24px, 6vw, 30px); line-height: 1.2; letter-spacing: -.03em; }}
    p {{ margin: 14px 0 0; color: #596579; font-size: 16px; line-height: 1.6; }}
    @media (max-width: 480px) {{ main {{ padding: 32px 22px; }} }}
  </style>
</head>
<body>
  <main>
    <p class="brand">TurnoverOps</p>
    <div class="icon" aria-hidden="true">{icon}</div>
    <h1>{message}</h1>
  </main>
</body>
</html>"""


@router.get("/public/cleaner-calendar/connect/{token}")
def connect_cleaner_calendar(token: str, settings: AppSettings, db: Db) -> Response:
    if not calendar_configured(settings):
        return HTMLResponse("<h1>Calendar connection is not configured.</h1>", status_code=503)
    url = authorization_url(db, token, settings, datetime.now(UTC))
    if url is None:
        return HTMLResponse("<h1>This calendar connection link has expired or was already used.</h1>", status_code=410)
    return RedirectResponse(url, status_code=302)


@router.get("/public/cleaner-calendar/callback", response_class=HTMLResponse)
def cleaner_calendar_callback(
    settings: AppSettings,
    db: Db,
    state: str | None = None,
    code: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    if not state:
        return HTMLResponse(
            _calendar_result_page("Calendar connection could not be verified.", success=False),
            status_code=400,
        )
    result = finish_authorization(
        db,
        state,
        code=code,
        error=error,
        settings=settings,
        now=datetime.now(UTC),
    )
    content = {
        "connected": "Google Calendar is connected. You can close this page.",
        "skipped": "Google Calendar was not connected. Your configured availability remains in use.",
        "invalid": "This calendar connection link expired or was already used.",
        "error": "Google Calendar could not be connected. The owner can send a new connection email.",
    }.get(result, "Google Calendar could not be connected.")
    status = 200 if result in {"connected", "skipped"} else 400
    return HTMLResponse(
        _calendar_result_page(content, success=result == "connected"),
        status_code=status,
    )


@router.get("/public/rejections/{token}")
def get_rejection_details(token: str, settings: AppSettings, db: Db) -> dict:
    return rejection_details(db, token, settings, datetime.now(UTC))


@router.post("/public/rejections/{token}")
def submit_rejection(token: str, settings: AppSettings, db: Db) -> dict:
    _status, result = rejection_confirm(db, token, settings, datetime.now(UTC), as_json=True)
    return result


@router.get("/reject/{token}", response_class=HTMLResponse)
def preview_rejection(token: str, settings: AppSettings, db: Db) -> HTMLResponse:
    status, body = rejection_preview(db, token, settings, datetime.now(UTC))
    return HTMLResponse(body, status_code=status)


@router.post("/reject/{token}", response_class=HTMLResponse)
def confirm_rejection(token: str, settings: AppSettings, db: Db) -> HTMLResponse:
    status, body = rejection_confirm(db, token, settings, datetime.now(UTC))
    return HTMLResponse(body, status_code=status)
