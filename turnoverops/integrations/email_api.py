"""Outgoing mail goes through Gmail SMTP. A missing setting is not a successful send."""

import html

from turnoverops.core.config import Settings
from turnoverops.integrations.smtp_email import SmtpError, SmtpNotConfigured, send_smtp_email


class EmailNotConfigured(Exception):
    pass


class EmailError(Exception):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def send_email(
    settings: Settings,
    *,
    to: str,
    subject: str,
    body: str,
    client=None,
) -> dict:
    del client
    try:
        return send_smtp_email(
            settings,
            to=to,
            subject=subject,
            text=body,
            html=f"<p>{html.escape(body)}</p>",
        )
    except SmtpNotConfigured as exc:
        raise EmailNotConfigured(str(exc)) from exc
    except SmtpError as exc:
        raise EmailError(str(exc)) from exc
