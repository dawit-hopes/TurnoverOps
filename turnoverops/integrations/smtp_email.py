"""Gmail SMTP transport. A missing setting or a refused message is not a successful send."""

import smtplib
import ssl
from email.message import EmailMessage

from turnoverops.core.config import Settings


class SmtpNotConfigured(Exception):
    pass


class SmtpError(Exception):
    def __init__(self, message: str, *, transient: bool) -> None:
        super().__init__(message)
        self.transient = transient


_REQUIRED = (
    ("smtp_host", "SMTP_HOST"),
    ("smtp_username", "SMTP_USERNAME"),
    ("smtp_password", "SMTP_PASSWORD"),
    ("email_from", "EMAIL_FROM"),
)


def validate_smtp_config(settings: Settings) -> None:
    missing = [label for attr, label in _REQUIRED if not str(getattr(settings, attr) or "").strip()]
    if not settings.smtp_port:
        missing.append("SMTP_PORT")
    if missing:
        raise SmtpNotConfigured(
            "SMTP is not configured. Set " + ", ".join(missing) + " before sending mail."
        )


def send_smtp_email(
    settings: Settings,
    *,
    to: str,
    subject: str,
    text: str,
    html: str,
    timeout: float = 20,
) -> dict:
    validate_smtp_config(settings)
    message = EmailMessage()
    message["From"] = settings.email_from
    message["To"] = to
    message["Subject"] = subject
    message.set_content(text)
    message.add_alternative(html, subtype="html")
    context = ssl.create_default_context()
    try:
        with smtplib.SMTP(settings.smtp_host, int(settings.smtp_port), timeout=timeout) as smtp:
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
            smtp.login(settings.smtp_username, settings.smtp_password)
            refused = smtp.send_message(message)
    except SmtpNotConfigured:
        raise
    except smtplib.SMTPAuthenticationError as exc:
        raise SmtpError(_smtp_text("SMTP authentication failed", exc), transient=False) from exc
    except smtplib.SMTPRecipientsRefused as exc:
        raise SmtpError(_smtp_text("SMTP refused the recipient", exc), transient=False) from exc
    except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError, TimeoutError, OSError) as exc:
        raise SmtpError(_smtp_text("SMTP connection failed", exc), transient=True) from exc
    except smtplib.SMTPException as exc:
        raise SmtpError(_smtp_text("SMTP failed", exc), transient=True) from exc
    if refused:
        raise SmtpError("SMTP refused one or more recipients", transient=False)
    return {"status": "sent"}


def _smtp_text(prefix: str, exc: Exception) -> str:
    detail = getattr(exc, "smtp_error", None) or exc
    if isinstance(detail, bytes):
        detail = detail.decode(errors="replace")
    return f"{prefix}: {detail}"[:300]
