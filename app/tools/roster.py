"""Read tools: the employee's trips and cab options (rule facts live in checks.py).
Every query is filtered by who.company_id (tenant isolation, NFR-3).
Data Flow: agent node + Identity(from JWT) -> query DB (own company only) -> plain dicts."""
from sqlalchemy import and_, func, select

from app.auth.jwt import Identity
from app.db import store
from app.db.schema import cabs, roster


def list_trips(who: Identity, from_day: str) -> list[dict]:
    """Data Flow [3]: Identity -> roster JOIN cabs (own company + own employee, from today on)
    -> [{date, trip_type, cab_id, depart_at, route}]."""
    q = (select(roster.c.date, roster.c.trip_type, roster.c.cab_id, cabs.c.depart_at, cabs.c.route)
         .join(cabs, and_(cabs.c.company_id == roster.c.company_id, cabs.c.id == roster.c.cab_id))
         .where(roster.c.company_id == who.company_id,    # tenant filter
                roster.c.employee_id == who.user_id,      # only this employee
                roster.c.date >= from_day)                # ISO dates sort as text
         .order_by(roster.c.date, cabs.c.depart_at))
    with store.engine.connect() as conn:
        return [dict(r) for r in conn.execute(q).mappings()]


def get_trip(who: Identity, day: str, trip: str) -> dict | None:
    """The employee's booked cab for that day/trip, or None."""
    q = (select(roster.c.cab_id, cabs.c.depart_at)
         .join(cabs, and_(cabs.c.company_id == roster.c.company_id, cabs.c.id == roster.c.cab_id))
         .where(roster.c.company_id == who.company_id,
                roster.c.employee_id == who.user_id,
                roster.c.date == day, roster.c.trip_type == trip))
    with store.engine.connect() as conn:
        row = conn.execute(q).mappings().first()
    return dict(row) if row else None


def list_options(who: Identity, day: str, trip: str) -> list[dict]:
    """All cabs of this company/trip with seats booked on that day."""
    booked = (select(func.count()).select_from(roster)
              .where(roster.c.company_id == cabs.c.company_id, roster.c.cab_id == cabs.c.id,
                     roster.c.date == day)
              .scalar_subquery())
    q = (select(cabs.c.id, cabs.c.depart_at, cabs.c.capacity, booked.label("booked"))
         .where(cabs.c.company_id == who.company_id, cabs.c.trip_type == trip)
         .order_by(cabs.c.depart_at))
    with store.engine.connect() as conn:
        return [dict(r) for r in conn.execute(q).mappings()]
