"""External crontab entry: python -m turnoverops.workers.daily

Runs the same maintenance pass as the in-process scheduler: feed polling, calendar
replanning, and the timezone-aware daily plan. Schedule it every few minutes.
There is no webhook.
"""

from turnoverops.core.config import get_settings
from turnoverops.core.db import make_engine, make_session_factory
from turnoverops.workers.jobs import run_maintenance


def main() -> None:
    settings = get_settings()
    engine = make_engine(settings.database_url)
    run_maintenance(make_session_factory(engine), settings)
    engine.dispose()


if __name__ == "__main__":
    main()
