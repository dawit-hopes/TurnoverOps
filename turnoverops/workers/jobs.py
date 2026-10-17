import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy.orm import Session

from turnoverops.core.config import Settings
from turnoverops.services.cleaner_mail import retry_smtp_notifications
from turnoverops.services.planning.daily import run_due_daily_plans
from turnoverops.services.planning.orchestrate import orchestrate
from turnoverops.services.sync import sync_due_icals
from turnoverops.services.workflow import expire_due_assignments

logger = logging.getLogger("turnoverops")


def run_maintenance(session_factory, settings: Settings) -> None:
    db = session_factory()
    try:
        now = datetime.now(UTC)
        expire_due_assignments(db, settings, now)
        retry_smtp_notifications(db, settings, now=now)
        pending = sync_due_icals(db, settings, now)
        db.commit()
        for host_id, task_ids in pending.items():
            unique = list(dict.fromkeys(task_ids))
            if not unique:
                continue
            try:
                orchestrate(db, host_id, "calendar_change", settings, task_ids=unique, now=now)
            except Exception:
                logger.exception("calendar_planning_failed")
        run_due_daily_plans(db, settings, now)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("maintenance_failed")
    finally:
        db.close()


def _run_scheduled_step(session_factory, label: str, step: Callable[[Session], None]) -> None:
    db = session_factory()
    started = time.monotonic()
    logger.info("scheduled_%s_started", label)
    try:
        step(db)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("scheduled_%s_failed", label)
    finally:
        db.close()
        logger.info("scheduled_%s_finished duration_seconds=%.2f", label, time.monotonic() - started)


def _expire_assignments(db, settings: Settings) -> None:
    expire_due_assignments(db, settings, datetime.now(UTC))


def _retry_notifications(db, settings: Settings) -> None:
    retry_smtp_notifications(db, settings, now=datetime.now(UTC))


def _sync_calendars(db, settings: Settings) -> None:
    now = datetime.now(UTC)
    pending = sync_due_icals(db, settings, now)
    db.commit()
    for host_id, task_ids in pending.items():
        unique = list(dict.fromkeys(task_ids))
        if not unique:
            continue
        try:
            orchestrate(db, host_id, "calendar_change", settings, task_ids=unique, now=now)
        except Exception:
            db.rollback()
            logger.exception("calendar_planning_failed")


def _daily_plans(db, settings: Settings) -> None:
    run_due_daily_plans(db, settings, datetime.now(UTC))


def start_scheduler(session_factory, settings: Settings) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="UTC")
    steps = {
        "expire_assignments": lambda db: _expire_assignments(db, settings),
        "retry_notifications": lambda db: _retry_notifications(db, settings),
        "sync_calendars": lambda db: _sync_calendars(db, settings),
        "daily_plans": lambda db: _daily_plans(db, settings),
    }
    for name, step in steps.items():
        scheduler.add_job(
            lambda step=step, name=name: _run_scheduled_step(session_factory, name, step),
            "interval",
            seconds=settings.scheduler_interval_seconds,
            id=name,
            name=f"TurnoverOps {name}",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
    scheduler.start()
    return scheduler
