"""Load the facts the rules need, straight from the DB (never from the LLM).
Every query is filtered by the token's company_id.
Data Flow: change dict + Identity -> [policy, roster row, cab times, seat count] -> rules.Facts"""
from sqlalchemy import func, select

from app import clock
from app.auth.jwt import Identity
from app.db import store
from app.db.schema import cabs, policies, roster
from app.rules.engine import trip_at
from app.rules.validate import Facts, validate


def load_facts(conn, who: Identity, ch: dict, lock: bool = False) -> Facts:
    """Read everything validate() needs, inside the caller's connection/transaction.
    lock=True also locks the new cab row (Postgres FOR UPDATE; SQLite is already locked)."""
    co, day, trip = who.company_id, ch["date"], ch["trip_type"]

    cutoff = conn.execute(select(policies.c.cutoff_minutes).where(
        policies.c.company_id == co, policies.c.trip_type == trip)).scalar_one()
    booked_cab = conn.execute(select(roster.c.cab_id).where(
        roster.c.company_id == co, roster.c.employee_id == who.user_id,
        roster.c.date == day, roster.c.trip_type == trip)).scalar()
    from_at = trip_at(day, _cab(conn, co, ch["from_cab"])["depart_at"])

    to_at = capacity = booked = None
    if ch["to_cab"]:
        new = _cab(conn, co, ch["to_cab"], lock)
        if new["trip_type"] != trip:
            raise ValueError("cab is for a different trip type")
        to_at, capacity = trip_at(day, new["depart_at"]), new["capacity"]
        booked = conn.execute(select(func.count()).select_from(roster).where(
            roster.c.company_id == co, roster.c.cab_id == new["id"], roster.c.date == day,
            roster.c.employee_id != who.user_id)).scalar_one()

    return Facts(clock.now(), cutoff, booked_cab, ch["from_cab"], from_at, to_at, capacity, booked)


def precheck(who: Identity, ch: dict) -> tuple[str | None, Facts]:
    """Agent's first check (no lock). Data Flow: decide node -> precheck -> load_facts -> validate.
    Returns (reason or None, facts) so the reply can explain the reason."""
    with store.engine.connect() as conn:
        facts = load_facts(conn, who, ch)
    return validate(facts), facts


def _cab(conn, co: str, cab_id: str, lock: bool = False) -> dict:
    """One cab of THIS company. Another company's cab id -> NoResultFound (isolation)."""
    q = select(cabs).where(cabs.c.company_id == co, cabs.c.id == cab_id)
    return dict(conn.execute(q.with_for_update() if lock else q).mappings().one())
