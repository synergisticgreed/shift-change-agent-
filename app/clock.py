"""Single source of 'now' so tests can freeze time, in the company's local timezone.
Data Flow: system clock (UTC in Docker) -> convert to settings.app_tz -> naive local time
           -> cut-off rules, "today/tomorrow", seed dates. Cab times ("18:00") are local, so
           'now' must be local too, whatever timezone the server/container runs in."""
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.config import settings


def _local() -> datetime:
    return datetime.now(ZoneInfo(settings.app_tz)).replace(tzinfo=None, second=0, microsecond=0)


def now() -> datetime:
    """'Now' for the rules. Tests/eval replace this to freeze time."""
    return _local()


def real_today() -> date:
    """Real local date, NOT affected by a frozen now(). Seed dates use it, so freezing
    the clock in one test never shifts the trips seeded for the next one."""
    return _local().date()
