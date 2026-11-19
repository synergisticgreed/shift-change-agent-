"""Business rules as pure functions (FR-5). No DB, no LLM.
Data Flow: values from tools (cab times, seat counts, cut-off) -> rules -> True/False -> decide/apply."""
from datetime import datetime, timedelta


def trip_at(day: str, hhmm: str) -> datetime:
    """'2026-11-21' + '20:00' -> datetime."""
    return datetime.fromisoformat(f"{day}T{hhmm}")


def check_cutoff(cutoff_minutes: int, scheduled_at: datetime, now: datetime) -> bool:
    """Change allowed only if made at least cutoff_minutes before the trip."""
    return scheduled_at - now >= timedelta(minutes=cutoff_minutes)


def has_seat(capacity: int, booked: int) -> bool:
    return booked < capacity


def minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def nearest_free(options: list[dict], target: str) -> dict | None:
    """Cab option (with a free seat) closest to the target time."""
    free = [o for o in options if has_seat(o["capacity"], o["booked"])]
    return min(free, key=lambda o: abs(minutes(o["depart_at"]) - minutes(target)), default=None)
