from datetime import UTC, datetime, time, timedelta

from turnoverops.scheduling.engine import BusyInterval, CleanerInput, JobWindow, decide
from turnoverops.scheduling.turns import Stay, build_turnovers
from zoneinfo import ZoneInfo


def cleaner(cleaner_id: str, **overrides) -> CleanerInput:
    data = dict(
        id=cleaner_id,
        name=cleaner_id,
        active=True,
        timezone="UTC",
        working_days=frozenset(range(7)),
        work_start=time(9, 0),
        work_end=time(17, 0),
        max_jobs_per_day=None,
        eligible_property_ids=None,
        preferred_property_ids=frozenset(),
    )
    data.update(overrides)
    return CleanerInput(**data)


def window(**overrides) -> JobWindow:
    data = dict(
        property_id="prop",
        property_timezone="UTC",
        window_start=datetime(2026, 11, 3, 10, tzinfo=UTC),
        window_end=datetime(2026, 11, 3, 15, tzinfo=UTC),
        duration_minutes=60,
    )
    data.update(overrides)
    return JobWindow(**data)


def test_same_inputs_select_the_same_cleaner():
    cleaners = [cleaner("b"), cleaner("a", preferred_property_ids=frozenset({"prop"}))]
    first = decide(cleaners, window(), [])
    second = decide(list(reversed(cleaners)), window(), [])
    assert first.selected.cleaner_id == second.selected.cleaner_id == "a"


def test_infeasible_cleaner_is_not_selected():
    decision = decide([cleaner("night", work_start=time(1, 0), work_end=time(2, 0))], window(), [])
    assert decision.selected is None
    assert decision.ranked_feasible == ()
    assert "outside_working_hours" in decision.rejected[0].reasons


def test_short_window_is_rejected():
    decision = decide([cleaner("a")], window(window_end=datetime(2026, 11, 3, 10, 30, tzinfo=UTC)), [])
    assert decision.selected is None
    assert "insufficient_window" in decision.rejected[0].reasons


def test_overlap_and_daily_limit():
    busy = [
        BusyInterval(
            cleaner_id="a",
            task_id="other",
            start=datetime(2026, 11, 3, 10, tzinfo=UTC),
            end=datetime(2026, 11, 3, 12, tzinfo=UTC),
        )
    ]
    shifted = decide([cleaner("a")], window(), busy)
    assert shifted.selected.scheduled_start == datetime(2026, 11, 3, 12, tzinfo=UTC)
    limited = decide(
        [cleaner("a", max_jobs_per_day=1)],
        window(window_start=datetime(2026, 11, 3, 13, tzinfo=UTC), window_end=datetime(2026, 11, 3, 16, tzinfo=UTC)),
        busy,
    )
    assert "daily_job_limit" in limited.rejected[0].reasons


def test_property_timezone_not_cleaner_timezone():
    job = JobWindow(
        property_id="prop",
        property_timezone="America/New_York",
        window_start=datetime(2026, 6, 15, 14, tzinfo=UTC),
        window_end=datetime(2026, 6, 15, 16, tzinfo=UTC),
        duration_minutes=60,
    )
    local = cleaner("local", timezone="America/New_York", work_start=time(9, 0), work_end=time(17, 0))
    distant = cleaner("distant", timezone="America/Los_Angeles", work_start=time(9, 0), work_end=time(12, 0))
    decision = decide([local, distant], job, [])
    assert decision.selected.cleaner_id == "local"
    assert "outside_working_hours" in next(item.reasons for item in decision.rejected if item.cleaner_id == "distant")
    assert decision.selected.scheduled_start == datetime(2026, 6, 15, 14, tzinfo=UTC)


def test_turnovers_are_stable_and_skip_cancelled():
    stays = [
        Stay("b", datetime(2026, 11, 3, 15, tzinfo=UTC), datetime(2026, 11, 5, 10, tzinfo=UTC)),
        Stay("a", datetime(2026, 11, 1, 15, tzinfo=UTC), datetime(2026, 11, 3, 10, tzinfo=UTC)),
        Stay("gone", datetime(2026, 11, 6, 15, tzinfo=UTC), datetime(2026, 11, 7, 10, tzinfo=UTC), True),
    ]
    specs = build_turnovers(stays, zone=ZoneInfo("UTC"), default_checkin=time(15, 0), duration_minutes=60)
    again = build_turnovers(stays, zone=ZoneInfo("UTC"), default_checkin=time(15, 0), duration_minutes=60)
    assert [spec.turnover_key for spec in specs] == [spec.turnover_key for spec in again]
    assert [spec.turnover_key for spec in specs] == ["a->b", "b->open"]
    assert specs[0].window_start == datetime(2026, 11, 3, 10, tzinfo=UTC)
    assert specs[0].window_end == datetime(2026, 11, 3, 15, tzinfo=UTC)
    assert specs[1].window_end - specs[1].window_start >= timedelta(minutes=60)
