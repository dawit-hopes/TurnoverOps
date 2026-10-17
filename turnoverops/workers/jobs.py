import logging
from datetime import UTC, datetime

from apscheduler.schedulers.background import BackgroundScheduler

from turnoverops.core.config import Settings
from turnoverops.services.notifications import retry_notifications
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
        retry_notifications(db, settings, now=now)
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


def start_scheduler(session_factory, settings: Settings) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        lambda: run_maintenance(session_factory, settings),
        "interval",
        seconds=settings.scheduler_interval_seconds,
        id="maintenance",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    scheduler.start()
    return scheduler
