"""The ONE place that decides if a change is allowed (FR-5). Pure code, no DB, no LLM.
Data Flow: tools.checks.load_check (DB facts) -> Facts -> validate() -> None (allowed) | reason
           Used twice: agent pre-check (booking.decide) and save-time re-check (requests.apply_change)."""
from dataclasses import dataclass
from datetime import datetime

from app.rules.engine import check_cutoff, has_seat


@dataclass(frozen=True)
class Facts:
    now: datetime
    cutoff_minutes: int        # company policy for this trip type
    booked_cab: str | None     # what the roster says the employee is on right now
    from_cab: str              # what the change thinks they are on
    from_at: datetime          # current trip departure
    to_at: datetime | None     # new trip departure (None = cancel)
    capacity: int | None       # new cab seats
    booked: int | None         # new cab seats taken, NOT counting this employee


def validate(f: Facts, override_cutoff: bool = False) -> str | None:
    """Checks run in this order. The first one that fails is the reason:
    1 stale       roster changed since we looked -> start over
    2 trip_passed a trip already departed        -> never allowed, not even by a manager
    3 cab_full    no free seat                   -> offer another cab (escalating is pointless)
    4 cutoff      too close to departure         -> manager may override (override_cutoff)"""
    if f.booked_cab != f.from_cab:
        return "stale"
    earliest = min(t for t in (f.from_at, f.to_at) if t)
    if earliest <= f.now:
        return "trip_passed"
    if f.to_at and not has_seat(f.capacity, f.booked):
        return "cab_full"
    if not override_cutoff and not check_cutoff(f.cutoff_minutes, earliest, f.now):
        return "cutoff"
    return None
