"""Cleaner assignment mail, weekly schedules, and one-time rejection links.

SMTP acceptance is recorded as sent. It is not treated as inbox delivery.
"""

from __future__ import annotations

import hashlib
import html
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from turnoverops.core.config import Settings, frontend_base_url
from turnoverops.core.security import hash_token
from turnoverops.domain import LIVE_ASSIGNMENT_STATES, LOCKED_TASK_STATUSES, AssignmentState, TaskStatus
from turnoverops.integrations.smtp_email import SmtpError, SmtpNotConfigured, send_smtp_email
from turnoverops.models import Assignment, Cleaner, CleaningTask, Host, Notification, Property, RejectionToken
from turnoverops.services.audit import audit
from turnoverops.services.cleaner_calendar import issue_connect_link
from turnoverops.services.host_policy import load_policy
from turnoverops.services.notifications import notify_host

_ASSIGNMENT = "assignment_email"
_WEEKLY = "weekly_schedule"
_MANAGER = "manager_rejection"


@dataclass
class MailResult:
    cleaner_id: str
    status: str
    jobs: int


def mail_zone(settings: Settings) -> ZoneInfo:
    name = (settings.app_timezone or "Africa/Addis_Ababa").strip() or "Africa/Addis_Ababa"
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("Africa/Addis_Ababa")


def assignment_fingerprint(assignment: Assignment, task: CleaningTask, prop: Property) -> str:
    raw = "|".join(
        [
            assignment.id,
            assignment.scheduled_start.isoformat(),
            assignment.scheduled_end.isoformat(),
            task.window_end.isoformat(),
            str(task.duration_minutes),
            prop.id,
            prop.name,
            task.instructions or "",
        ]
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def assignment_dedupe(assignment_id: str, fingerprint: str) -> str:
    return f"assignment-email:{assignment_id}:{fingerprint}"


def google_calendar_url(
    *,
    title: str,
    start: datetime,
    end: datetime,
    location: str,
    details: str,
    timezone: str = "Africa/Addis_Ababa",
) -> str:
    zone = ZoneInfo(timezone)
    dates = f"{_calendar_stamp(start, zone)}/{_calendar_stamp(end, zone)}"
    query = urlencode(
        {
            "action": "TEMPLATE",
            "text": title,
            "dates": dates,
            "ctz": timezone,
            "location": location,
            "details": details,
        },
        quote_via=quote,
    )
    return f"https://calendar.google.com/calendar/render?{query}"


def revoke_rejection_tokens(db: Session, assignment_id: str, now: datetime) -> None:
    rows = db.scalars(
        select(RejectionToken).where(
            RejectionToken.assignment_id == assignment_id,
            RejectionToken.used_at.is_(None),
            RejectionToken.revoked_at.is_(None),
        )
    ).all()
    for row in rows:
        row.revoked_at = now


def issue_rejection_token(db: Session, assignment: Assignment, settings: Settings, now: datetime) -> str:
    revoke_rejection_tokens(db, assignment.id, now)
    raw = secrets.token_urlsafe(32)
    db.add(
        RejectionToken(
            host_id=assignment.host_id,
            assignment_id=assignment.id,
            cleaner_id=assignment.cleaner_id,
            token_hash=hash_token(raw),
            expires_at=now + timedelta(hours=settings.rejection_token_ttl_hours),
        )
    )
    db.flush()
    return raw


def send_assignment_email(db: Session, assignment: Assignment, settings: Settings, now: datetime) -> Notification | None:
    task = db.get(CleaningTask, assignment.task_id)
    prop = db.get(Property, task.property_id) if task is not None else None
    cleaner = db.get(Cleaner, assignment.cleaner_id)
    if task is None or prop is None or cleaner is None:
        return None
    if not load_policy(db, assignment.host_id).notify_cleaners:
        return None
    if task.current_assignment_id != assignment.id or assignment.state not in LIVE_ASSIGNMENT_STATES:
        return None
    fingerprint = assignment_fingerprint(assignment, task, prop)
    dedupe = assignment_dedupe(assignment.id, fingerprint)
    existing = _notification(db, assignment.host_id, dedupe)
    if existing is not None and existing.status == "sent":
        return existing
    note = existing or _new_notification(
        db,
        host_id=assignment.host_id,
        recipient_id=cleaner.id,
        task_id=task.id,
        message_type=_ASSIGNMENT,
        dedupe_key=dedupe,
        body=f"Assignment email for {prop.name}",
    )
    if not cleaner.email:
        _finish(db, note, "failed", "Cleaner has no email address", now, transient=False, settings=settings)
        return note
    token = issue_rejection_token(db, assignment, settings, now)
    subject, text, html_body = render_assignment_email(cleaner, prop, task, assignment, token, settings)
    note.body = text[:2000]
    _deliver(db, note, settings, now, to=cleaner.email, subject=subject, text=text, html=html_body, via="assignment")
    return note


def send_weekly_schedules(
    db: Session,
    settings: Settings,
    now: datetime,
    *,
    host_id: str | None = None,
    assignment_ids: list[str] | None = None,
) -> list[MailResult]:
    zone = mail_zone(settings)
    start, end, week_label = _week_bounds(now, zone)
    stmt = select(Assignment).where(Assignment.state.in_(tuple(LIVE_ASSIGNMENT_STATES)))
    if host_id is not None:
        stmt = stmt.where(Assignment.host_id == host_id)
    if assignment_ids is not None:
        if not assignment_ids:
            return []
        stmt = stmt.where(Assignment.id.in_(assignment_ids))
    grouped: dict[str, list[tuple[Assignment, CleaningTask, Property]]] = {}
    for assignment in db.scalars(stmt).all():
        if assignment_ids is None and not (start <= assignment.scheduled_start < end):
            continue
        task = db.get(CleaningTask, assignment.task_id)
        prop = db.get(Property, task.property_id) if task is not None else None
        if task is None or prop is None or task.current_assignment_id != assignment.id:
            continue
        if task.status in {TaskStatus.completed, TaskStatus.blocked}:
            continue
        grouped.setdefault(assignment.cleaner_id, []).append((assignment, task, prop))
    results: list[MailResult] = []
    for cleaner_id, jobs in grouped.items():
        cleaner = db.get(Cleaner, cleaner_id)
        if cleaner is None:
            continue
        if not load_policy(db, cleaner.host_id).notify_cleaners:
            continue
        jobs.sort(key=lambda item: item[0].scheduled_start)
        schedule_hash = hashlib.sha256(
            "|".join(assignment_fingerprint(assignment, task, prop) for assignment, task, prop in jobs).encode()
        ).hexdigest()[:32]
        dedupe = f"weekly:{cleaner.id}:{week_label}:{schedule_hash}"[:255]
        existing = _notification(db, cleaner.host_id, dedupe)
        if existing is not None and existing.status == "sent":
            results.append(MailResult(cleaner.id, "sent", len(jobs)))
            continue
        note = existing or _new_notification(
            db,
            host_id=cleaner.host_id,
            recipient_id=cleaner.id,
            task_id=jobs[0][1].id,
            message_type=_WEEKLY,
            dedupe_key=dedupe,
            body=f"Weekly schedule {week_label}",
        )
        if not cleaner.email:
            _finish(db, note, "failed", "Cleaner has no email address", now, transient=False, settings=settings)
            results.append(MailResult(cleaner.id, note.status, len(jobs)))
            continue
        rendered_jobs = []
        for assignment, task, prop in jobs:
            token = issue_rejection_token(db, assignment, settings, now)
            rendered_jobs.append(_job_view(cleaner, prop, task, assignment, token, settings))
        subject, text, html_body = render_weekly_email(cleaner, rendered_jobs, week_label, settings)
        note.body = text[:2000]
        _deliver(db, note, settings, now, to=cleaner.email, subject=subject, text=text, html=html_body, via="weekly")
        if note.status == "sent":
            for assignment, task, prop in jobs:
                fingerprint = assignment_fingerprint(assignment, task, prop)
                cover_key = assignment_dedupe(assignment.id, fingerprint)
                cover = _notification(db, cleaner.host_id, cover_key)
                if cover is None:
                    cover = _new_notification(
                        db,
                        host_id=cleaner.host_id,
                        recipient_id=cleaner.id,
                        task_id=task.id,
                        message_type=_ASSIGNMENT,
                        dedupe_key=cover_key,
                        body=f"Included in the weekly schedule for {week_label}",
                    )
                cover.status = "sent"
                cover.provider_response = {"status": "sent", "via": "weekly"}
                cover.next_retry_at = None
        results.append(MailResult(cleaner.id, note.status, len(jobs)))
    return results


def retry_smtp_notifications(db: Session, settings: Settings, *, now: datetime) -> int:
    rows = db.scalars(
        select(Notification).where(
            Notification.channel == "smtp",
            Notification.status == "failed",
            Notification.retry_count < settings.max_notification_retries,
            Notification.next_retry_at.is_not(None),
            Notification.next_retry_at <= now,
        )
    ).all()
    delivered = 0
    for note in rows:
        before = note.status
        if note.message_type == _ASSIGNMENT and note.dedupe_key and note.dedupe_key.startswith("assignment-email:"):
            assignment_id = note.dedupe_key.split(":")[1]
            assignment = db.get(Assignment, assignment_id)
            task = db.get(CleaningTask, assignment.task_id) if assignment is not None else None
            prop = db.get(Property, task.property_id) if task is not None else None
            if assignment is None or task is None or prop is None:
                note.next_retry_at = None
            elif note.dedupe_key != assignment_dedupe(assignment.id, assignment_fingerprint(assignment, task, prop)):
                note.next_retry_at = None
            else:
                send_assignment_email(db, assignment, settings, now)
        elif note.message_type == _WEEKLY:
            send_weekly_schedules(db, settings, now, host_id=note.host_id)
        elif note.message_type == _MANAGER and note.dedupe_key and note.dedupe_key.startswith("manager-rejection:"):
            assignment = db.get(Assignment, note.dedupe_key.split(":", 1)[1])
            if assignment is not None:
                email_manager_rejection(db, assignment, settings, now, reason=note.body[:500])
        if note.status == "sent" and before != "sent":
            delivered += 1
        db.flush()
    return delivered


def retry_task_assignment_email(db: Session, task: CleaningTask, settings: Settings, now: datetime) -> Notification | None:
    if not task.current_assignment_id:
        return None
    assignment = db.get(Assignment, task.current_assignment_id)
    if assignment is None:
        return None
    return send_assignment_email(db, assignment, settings, now)


def rejection_preview(db: Session, raw_token: str, settings: Settings, now: datetime) -> tuple[int, str]:
    found = _resolve_token(db, raw_token, settings, now)
    if found is None:
        return 404, _page("This link is not valid.", "Check the email for the latest link. Nothing was changed.")
    token, assignment, task, prop, blocker = found
    if token.used_at is not None:
        return 200, _page("This decline was already recorded.", "Nothing was changed.")
    if blocker or assignment is None or task is None or prop is None:
        return 200, _page(blocker or "This link is not valid.", "Nothing was changed.")
    return 200, _confirm_page(prop, task, assignment, raw_token, settings)


def rejection_details(db: Session, raw_token: str, settings: Settings, now: datetime) -> dict:
    found = _resolve_token(db, raw_token, settings, now)
    if found is None:
        return {"can_decline": False, "title": "This link is not valid", "detail": "Check the email for the latest link. Nothing was changed."}
    token, assignment, task, prop, blocker = found
    if token.used_at is not None:
        return {"can_decline": False, "title": "This decline was already recorded", "detail": "Nothing was changed."}
    if blocker or assignment is None or task is None or prop is None:
        return {"can_decline": False, "title": blocker or "This link is not valid", "detail": "Nothing was changed."}
    return {
        "can_decline": True,
        "title": "Review this cleaning",
        "detail": "This job will only be declined if you press the button below.",
        "property_name": prop.name,
        "scheduled_start": assignment.scheduled_start.isoformat(),
        "scheduled_end": assignment.scheduled_end.isoformat(),
        "timezone": mail_zone(settings).key,
    }


def rejection_confirm(db: Session, raw_token: str, settings: Settings, now: datetime, *, as_json: bool = False) -> tuple[int, str | dict]:
    found = _resolve_token(db, raw_token, settings, now)
    if found is None:
        return 404, _rejection_response("This link is not valid", "Nothing was changed.", as_json)
    token, assignment, task, prop, blocker = found
    if token.used_at is not None:
        return 200, _rejection_response("This decline was already recorded", "The job was not changed again.", as_json)
    if blocker or assignment is None or task is None or prop is None:
        return 200, _rejection_response(blocker or "This link is not valid", "Nothing was changed.", as_json)
    from turnoverops.services.workflow import close_assignment

    token.used_at = now
    close_assignment(
        db,
        assignment,
        now,
        settings,
        state=AssignmentState.declined,
        actor="cleaner",
        reason="Cleaner declined this job from the email link",
    )
    db.refresh(task)
    email_manager_rejection(db, assignment, settings, now, reason="Cleaner declined this job from the email link")
    replacement = _replacement_name(db, task, assignment.id)
    if replacement:
        detail = f"{replacement} is now assigned. You do not need to do anything else."
    else:
        detail = "No replacement was assigned. The manager has been told."
    return 200, _rejection_response(f"You declined the cleaning at {prop.name}", detail, as_json)


def _rejection_response(title: str, detail: str, as_json: bool) -> str | dict:
    return {"title": title, "detail": detail, "can_decline": False} if as_json else _page(title, detail)


def email_manager_rejection(db: Session, assignment: Assignment, settings: Settings, now: datetime, *, reason: str) -> Notification | None:
    task = db.get(CleaningTask, assignment.task_id)
    prop = db.get(Property, task.property_id) if task is not None else None
    host = db.get(Host, assignment.host_id)
    cleaner = db.get(Cleaner, assignment.cleaner_id)
    if task is None or prop is None or host is None:
        return None
    replacement = _replacement_name(db, task, assignment.id)
    why = ""
    if replacement is None:
        why = str((task.conflict_details or {}).get("summary") or "No feasible cleaner can take this turnover")
    zone = mail_zone(settings)
    when = _format_when(assignment.scheduled_start, zone)
    deadline = _format_when(task.window_end, zone)
    cleaner_name = cleaner.name if cleaner is not None else "The cleaner"
    notify_host(
        db,
        host.id,
        "assignment_rejected",
        f"{cleaner_name} declined {prop.name}. " + (f"{replacement} is assigned." if replacement else why),
        task_id=task.id,
        dedupe_key=f"rejection-dashboard:{assignment.id}",
    )
    dedupe = f"manager-rejection:{assignment.id}"
    existing = _notification(db, host.id, dedupe)
    if existing is not None and existing.status == "sent":
        return existing
    subject = f"Cleaner declined — {prop.name} — {_subject_date(assignment.scheduled_start, zone)}"
    lines = [
        f"{cleaner_name} declined the cleaning at {prop.name}.",
        f"When: {when}",
        f"Deadline: {deadline} ({_zone_label(zone)})",
        f"Reason: {reason}",
    ]
    if replacement:
        lines.append(f"Replacement: {replacement} is assigned. A separate email was prepared for them.")
    else:
        lines.append(f"No replacement was found. {why}")
        lines.append("The cleaning task was left unresolved. A new cleaning task was not created.")
    text = "\n".join(lines)
    html_body = "".join(f"<p>{html.escape(line)}</p>" for line in lines)
    note = existing or _new_notification(
        db,
        host_id=host.id,
        recipient_id=host.id,
        task_id=task.id,
        message_type=_MANAGER,
        dedupe_key=dedupe,
        body=text[:2000],
        recipient_type="host",
    )
    note.body = text[:2000]
    if not host.email:
        _finish(db, note, "failed", "Manager has no email address", now, transient=False, settings=settings)
        return note
    _deliver(db, note, settings, now, to=host.email, subject=subject, text=text, html=html_body, via="manager")
    return note


def render_assignment_email(cleaner, prop, task, assignment, token: str, settings: Settings) -> tuple[str, str, str]:
    job = _job_view(cleaner, prop, task, assignment, token, settings)
    zone_name = mail_zone(settings).key
    subject = f"Cleaning Assignment — {prop.name} — {job['date']}"
    text_lines = [
        f"Hello {cleaner.name},",
        "",
        f"You are assigned this cleaning at {prop.name}. It is already confirmed.",
        "You only need to act if you cannot do it. There is no accept step.",
        "",
        f"Property: {prop.name}",
        f"Date: {job['date']}",
        f"Start: {job['start']}",
        f"End: {job['end']}",
        f"Duration: {job['duration']}",
        f"Deadline: {job['deadline']}",
        f"Times are shown in {_zone_label(mail_zone(settings))}.",
    ]
    if job["instructions"]:
        text_lines.extend(["", f"Instructions: {job['instructions']}"])
    text_lines.extend(
        [
            "",
            "Add this job to Google Calendar (this link opens a form; it does not add the event for you):",
            job["calendar_url"],
            "",
            "Reject this job if you cannot do it:",
            job["reject_url"],
        ]
    )
    text = "\n".join(text_lines)
    instructions = ""
    if job["instructions"]:
        instructions = f"<p><strong>Instructions:</strong> {html.escape(job['instructions'])}</p>"
    html_body = f"""
    <p>Hello {html.escape(cleaner.name)},</p>
    <p>You are assigned this cleaning at {html.escape(prop.name)}. It is already confirmed.
    You only need to act if you cannot do it. There is no accept step.</p>
    <ul>
      <li>Property: {html.escape(prop.name)}</li>
      <li>Date: {html.escape(job['date'])}</li>
      <li>Start: {html.escape(job['start'])}</li>
      <li>End: {html.escape(job['end'])}</li>
      <li>Duration: {html.escape(job['duration'])}</li>
      <li>Deadline: {html.escape(job['deadline'])}</li>
    </ul>
    <p>Times are shown in {html.escape(_zone_label(mail_zone(settings)))}.</p>
    {instructions}
    <p><a href="{html.escape(job['calendar_url'])}">Add to Google Calendar</a></p>
    <p>The calendar link opens a prefilled form. It does not add the event for you.</p>
    <p><a href="{html.escape(job['reject_url'])}">Reject this job</a></p>
    """
    return subject, text, html_body


def render_weekly_email(cleaner, jobs: list[dict], week_label: str, settings: Settings) -> tuple[str, str, str]:
    zone_name = mail_zone(settings).key
    total_minutes = sum(job["duration_minutes"] for job in jobs)
    subject = f"Cleaning schedule — {week_label}"
    text = [
        f"Hello {cleaner.name},",
        "",
        f"Your cleaning schedule for {week_label}.",
        f"Jobs: {len(jobs)}",
        f"Total time: {_format_duration(total_minutes)}",
        f"Times are shown in {_zone_label(mail_zone(settings))}. Empty days are omitted.",
        "Each job is already assigned. Act only if you cannot do one. There is no accept step.",
        "",
    ]
    html_parts = [
        f"<p>Hello {html.escape(cleaner.name)},</p>",
        f"<p>Your cleaning schedule for {html.escape(week_label)}.</p>",
        f"<p>Jobs: {len(jobs)}. Total time: {html.escape(_format_duration(total_minutes))}.</p>",
        f"<p>Times are shown in {html.escape(_zone_label(mail_zone(settings)))}. Empty days are omitted.</p>",
        "<p>Each job is already assigned. Act only if you cannot do one. There is no accept step.</p>",
    ]
    current_day = None
    for job in jobs:
        if job["day_key"] != current_day:
            current_day = job["day_key"]
            text.extend([job["day_label"], ""])
            html_parts.append(f"<h2>{html.escape(job['day_label'])}</h2>")
        text.extend(
            [
                f"{job['start']}–{job['end']} · {job['property']}",
                f"Duration {job['duration']}. Deadline {job['deadline']}.",
            ]
        )
        if job["instructions"]:
            text.append(f"Instructions: {job['instructions']}")
        text.extend(["Add to Google Calendar:", job["calendar_url"], "Reject this job:", job["reject_url"], ""])
        instructions = f"<br>Instructions: {html.escape(job['instructions'])}" if job["instructions"] else ""
        html_parts.append(
            "<p>"
            f"{html.escape(job['start'])}–{html.escape(job['end'])} · {html.escape(job['property'])}<br>"
            f"Duration {html.escape(job['duration'])}. Deadline {html.escape(job['deadline'])}."
            f"{instructions}<br>"
            f"<a href=\"{html.escape(job['calendar_url'])}\">Add to Google Calendar</a><br>"
            f"<a href=\"{html.escape(job['reject_url'])}\">Reject this job</a>"
            "</p>"
        )
    return subject, "\n".join(text), "".join(html_parts)


def _job_view(cleaner, prop, task, assignment, token: str, settings: Settings) -> dict:
    zone = mail_zone(settings)
    start = assignment.scheduled_start.astimezone(zone)
    end = assignment.scheduled_end.astimezone(zone)
    deadline = task.window_end.astimezone(zone)
    instructions = (task.instructions or "").strip()
    calendar = google_calendar_url(
        title=f"Cleaning at {prop.name}",
        start=assignment.scheduled_start,
        end=assignment.scheduled_end,
        location=prop.name or "",
        details=(
            f"Assigned cleaning at {prop.name}. "
            "This link only opens a calendar form. "
            "If you cannot do the job, use the reject link in the email."
        ),
        timezone=zone.key,
    )
    return {
        "property": prop.name,
        "date": _subject_date(assignment.scheduled_start, zone),
        "day_key": start.date().isoformat(),
        "day_label": _day_label(start),
        "start": _clock(start),
        "end": _clock(end),
        "deadline": f"{_day_label(deadline)} at {_clock(deadline)}",
        "duration": _format_duration(task.duration_minutes),
        "duration_minutes": task.duration_minutes,
        "instructions": instructions,
        "calendar_url": calendar,
        "reject_url": f"{frontend_base_url(settings)}/reject/{token}",
    }


def _day_label(moment: datetime) -> str:
    return f"{moment.strftime('%A')}, {moment.day} {moment.strftime('%B %Y')}"


def _clock(moment: datetime) -> str:
    hour = moment.hour % 12 or 12
    suffix = "AM" if moment.hour < 12 else "PM"
    return f"{hour}:{moment.minute:02d} {suffix}"


def _zone_label(zone: ZoneInfo) -> str:
    name = zone.key.rsplit("/", 1)[-1].replace("_", " ")
    return "UTC" if name == "UTC" else f"{name} time"


def _resolve_token(db: Session, raw_token: str, settings: Settings, now: datetime):
    if not raw_token:
        return None
    token = db.scalar(select(RejectionToken).where(RejectionToken.token_hash == hash_token(raw_token)))
    if token is None:
        return None
    assignment = db.get(Assignment, token.assignment_id)
    task = db.get(CleaningTask, assignment.task_id) if assignment is not None else None
    prop = db.get(Property, task.property_id) if task is not None else None
    if assignment is None or task is None or prop is None:
        return token, assignment, task, prop, "This link is not valid."
    if token.used_at is not None:
        return token, assignment, task, prop, None
    if assignment.cleaner_id != token.cleaner_id:
        return token, assignment, task, prop, "This link does not belong to the assigned cleaner."
    if task.current_assignment_id != assignment.id or assignment.state not in LIVE_ASSIGNMENT_STATES:
        return token, assignment, task, prop, "This assignment was replaced. The newer assignment was not changed."
    if task.status in LOCKED_TASK_STATUSES or task.status == TaskStatus.blocked or task.started_at is not None:
        return token, assignment, task, prop, "This job can no longer be declined."
    cleaner = db.get(Cleaner, token.cleaner_id)
    if cleaner is None or not cleaner.active:
        return token, assignment, task, prop, "This cleaner can no longer decline the job. Nothing was changed."
    if token.revoked_at is not None or token.expires_at <= now:
        return token, assignment, task, prop, "This link has expired."
    return token, assignment, task, prop, None


def _confirm_page(prop, task, assignment, raw_token: str, settings: Settings) -> str:
    zone = mail_zone(settings)
    start = assignment.scheduled_start.astimezone(zone)
    end = assignment.scheduled_end.astimezone(zone)
    body = (
        f"<p>Decline only this cleaning at {html.escape(prop.name)}.</p>"
        f"<p>{html.escape(_day_label(start))} · {_clock(start)}–{_clock(end)} "
        f"({html.escape(_zone_label(zone))})</p>"
        "<p>This page does not decline the job until you submit the form.</p>"
        f"<form method=\"post\" action=\"{_base_url(settings)}/reject/{html.escape(raw_token)}\">"
        "<button type=\"submit\">Reject this job</button></form>"
    )
    return _document("Reject this job", body)


def _page(title: str, detail: str) -> str:
    return _document(title, f"<p>{html.escape(title)}</p><p>{html.escape(detail)}</p>")


def _document(title: str, body: str) -> str:
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        f"<title>{html.escape(title)}</title></head><body>{body}</body></html>"
    )


def _replacement_name(db: Session, task: CleaningTask, declined_assignment_id: str) -> str | None:
    if not task.current_assignment_id or task.current_assignment_id == declined_assignment_id:
        return None
    if task.status not in {TaskStatus.confirmed, TaskStatus.awaiting_confirmation, TaskStatus.in_progress}:
        return None
    current = db.get(Assignment, task.current_assignment_id)
    if current is None or current.state not in LIVE_ASSIGNMENT_STATES:
        return None
    cleaner = db.get(Cleaner, current.cleaner_id)
    return cleaner.name if cleaner is not None else None


def _notification(db: Session, host_id: str, dedupe_key: str) -> Notification | None:
    return db.scalar(select(Notification).where(Notification.host_id == host_id, Notification.dedupe_key == dedupe_key))


def send_cleaner_welcome_email(
    db: Session,
    cleaner: Cleaner,
    settings: Settings,
    now: datetime | None = None,
) -> Notification | None:
    if not cleaner.email:
        return None
    dedupe_key = f"cleaner-welcome:{cleaner.id}"
    existing = _notification(db, cleaner.host_id, dedupe_key)
    if existing is not None:
        return existing
    moment = now or datetime.now(UTC)
    connect_url = issue_connect_link(db, cleaner, settings, moment)
    if connect_url:
        connect_text = f"Connect Google Calendar to let TurnoverOps check your busy times:\n{connect_url}"
        connect_html = (
            '<p>Connect Google Calendar to let TurnoverOps check your busy times.</p>'
            f'<p><a href="{html.escape(connect_url, quote=True)}">Connect Google Calendar</a></p>'
        )
    else:
        connect_text = "Google Calendar connection is not configured yet. Your configured working hours will be used until it is available."
        connect_html = f"<p>{html.escape(connect_text)}</p>"
    body = f"Welcome to TurnoverOps, {cleaner.name}.\n\n{connect_text}"
    note = _new_notification(
        db,
        host_id=cleaner.host_id,
        recipient_id=cleaner.id,
        task_id=None,
        message_type="cleaner_welcome",
        dedupe_key=dedupe_key,
        body=body,
    )
    name = html.escape(cleaner.name)
    _deliver(
        db,
        note,
        settings,
        moment,
        to=cleaner.email,
        subject="Welcome to TurnoverOps",
        text=body,
        html=f"<p>Hello {name}, welcome to TurnoverOps.</p>{connect_html}",
        via="gmail_smtp",
    )
    return note


def _new_notification(
    db: Session,
    *,
    host_id: str,
    recipient_id: str,
    task_id: str | None,
    message_type: str,
    dedupe_key: str,
    body: str,
    recipient_type: str = "cleaner",
) -> Notification:
    note = Notification(
        host_id=host_id,
        recipient_type=recipient_type,
        recipient_id=recipient_id,
        task_id=task_id,
        channel="smtp",
        message_type=message_type,
        body=body[:2000],
        status="pending",
        dedupe_key=dedupe_key,
    )
    db.add(note)
    db.flush()
    return note


def _deliver(
    db: Session,
    note: Notification,
    settings: Settings,
    now: datetime,
    *,
    to: str,
    subject: str,
    text: str,
    html: str,
    via: str,
) -> None:
    try:
        send_smtp_email(settings, to=to, subject=subject, text=text, html=html)
    except SmtpNotConfigured as exc:
        _finish(db, note, "not_configured", str(exc), now, transient=False, settings=settings)
    except SmtpError as exc:
        _finish(db, note, "failed", str(exc), now, transient=exc.transient, settings=settings)
    except Exception as exc:
        _finish(db, note, "failed", f"SMTP failed: {exc}"[:300], now, transient=True, settings=settings)
    else:
        note.status = "sent"
        note.next_retry_at = None
        note.provider_response = {"status": "sent", "via": via}
        audit(
            db,
            host_id=note.host_id,
            event_type="notification_sent",
            entity_type="notification",
            entity_id=note.id,
            reason="The email was accepted for sending.",
            details={"channel": "smtp", "message_type": note.message_type, "status": "sent"},
        )


def _finish(db, note, status: str, error: str, now: datetime, *, transient: bool, settings: Settings) -> None:
    note.status = status
    note.retry_count = (note.retry_count or 0) + (1 if status == "failed" else 0)
    if status == "not_configured":
        note.retry_count = settings.max_notification_retries
        note.next_retry_at = None
    elif status == "failed" and transient and note.retry_count < settings.max_notification_retries:
        note.next_retry_at = now + timedelta(seconds=settings.notification_retry_seconds * note.retry_count)
    else:
        note.next_retry_at = None
    note.provider_response = {"error": error[:300], "status": status}
    audit(
        db,
        host_id=note.host_id,
        event_type="notification_failed",
        entity_type="notification",
        entity_id=note.id,
        reason="Email is not set up, so the message was not sent." if status == "not_configured" else "The email could not be sent.",
        details={"channel": "smtp", "message_type": note.message_type, "status": status},
    )


def _base_url(settings: Settings) -> str:
    return (settings.app_base_url or "http://localhost:8000").rstrip("/")


def _calendar_stamp(moment: datetime, zone: ZoneInfo) -> str:
    local = moment.astimezone(zone)
    return local.strftime("%Y%m%dT%H%M%S")


def _subject_date(moment: datetime, zone: ZoneInfo) -> str:
    local = moment.astimezone(zone)
    return f"{local.day} {local.strftime('%b %Y')}"


def _format_when(moment: datetime, zone: ZoneInfo) -> str:
    local = moment.astimezone(zone)
    return f"{_day_label(local)} at {_clock(local)}"


def _format_duration(minutes: int) -> str:
    hours, mins = divmod(int(minutes), 60)
    if hours and mins:
        return f"{hours} h {mins} min"
    if hours:
        return f"{hours} h" if hours != 1 else "1 hour"
    return f"{mins} min"


def _week_bounds(now: datetime, zone: ZoneInfo) -> tuple[datetime, datetime, str]:
    local_date = now.astimezone(zone).date()
    monday = local_date - timedelta(days=local_date.weekday())
    sunday = monday + timedelta(days=6)
    start = datetime.combine(monday, time.min, tzinfo=zone).astimezone(UTC)
    end = datetime.combine(monday + timedelta(days=7), time.min, tzinfo=zone).astimezone(UTC)
    label = f"{monday.day} {monday.strftime('%b %Y')} – {sunday.day} {sunday.strftime('%b %Y')}"
    return start, end, label

