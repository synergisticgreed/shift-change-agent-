"""Read tools: the employee's trip, cab options, company policy.
Every query is filtered by who.company_id (tenant isolation, NFR-3).
Data Flow: agent node + Identity(from JWT) -> query DB (own company only) -> plain dicts."""
from sqlalchemy import and_, func, select

from app.auth.jwt import Identity
from app.db import store
from app.db.schema import cabs, policies, roster


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


def get_cutoff(who: Identity, trip: str) -> int:
    q = select(policies.c.cutoff_minutes).where(
        policies.c.company_id == who.company_id, policies.c.trip_type == trip)
    with store.engine.connect() as conn:
        return conn.execute(q).scalar_one()
