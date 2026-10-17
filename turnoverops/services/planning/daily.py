"""Timezone-aware daily planning. A second run on the same local date is recorded and skipped."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from turnoverops.core.config import Settings
from turnoverops.models import HostSettings, ScheduledJobRun
from turnoverops.services.planning.orchestrate import orchestrate


def run_due_daily_plans(db: Session, settings: Settings, now: datetime | None = None, *, client=None) -> int:
    moment = now or datetime.now(UTC)
    rows = db.scalars(
        select(HostSettings).where(
            HostSettings.daily_planning_enabled.is_(True),
            HostSettings.plan_approved_at.is_not(None),
        )
    ).all()
    started = 0
    for row in rows:
        local = moment.astimezone(ZoneInfo(row.timezone))
        if local.time() < row.daily_plan_time:
            continue
        operational_date = local.date().isoformat()
        completed = db.scalar(
            select(ScheduledJobRun).where(
                ScheduledJobRun.host_id == row.host_id,
                ScheduledJobRun.job_name == "daily_plan",
                ScheduledJobRun.operational_date == operational_date,
                ScheduledJobRun.status == "completed",
            )
        )
        if completed is not None:
            db.add(
                ScheduledJobRun(
                    host_id=row.host_id,
                    job_name="daily_plan",
                    operational_date=operational_date,
                    status="skipped",
                    started_at=moment,
                    finished_at=moment,
                    details={"llm_calls": 0, "reason": "already_completed"},
                )
            )
            db.commit()
            continue
        job = ScheduledJobRun(
            host_id=row.host_id,
            job_name="daily_plan",
            operational_date=operational_date,
            status="running",
            started_at=moment,
        )
        db.add(job)
        db.commit()
        try:
            run = orchestrate(db, row.host_id, "daily", settings, client=client, now=moment)
        except Exception as exc:
            job.status = "failed"
            job.error = str(exc)[:500]
            job.finished_at = datetime.now(UTC)
            db.commit()
            continue
        job.status = "completed"
        job.finished_at = datetime.now(UTC)
        job.details = {"planning_run_id": run.id, "llm_calls": run.llm_calls}
        db.commit()
        started += 1
    return started
