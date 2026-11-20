"""Cut-offs use the company's local time, not the server's (Docker runs in UTC).
Data Flow (tested): system clock -> clock.now() in settings.app_tz -> rules."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app import clock
from app.config import settings


def test_now_is_in_app_timezone_not_server_timezone(monkeypatch):
    monkeypatch.setattr(settings, "app_tz", "Asia/Kolkata")
    ist = clock.now()
    monkeypatch.setattr(settings, "app_tz", "UTC")
    utc = clock.now()
    assert abs((ist - utc) - timedelta(hours=5, minutes=30)) < timedelta(minutes=2)
    assert ist.tzinfo is None   # naive local time, comparable with "18:00" cab times


def test_real_today_ignores_a_frozen_now(monkeypatch):
    monkeypatch.setattr(clock, "now", lambda: datetime(2000, 1, 1, 9, 0))
    assert clock.real_today() == datetime.now(ZoneInfo(settings.app_tz)).date()
