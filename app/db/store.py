"""DB engine, create + seed, audit helper.
Data Flow: settings.database_url -> engine -> tools/* open transactions on it.
           data/seed.json -> _seed -> tables (first start only)."""
import json
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import create_engine, event, insert, select
from sqlalchemy.engine import Connection, Engine

from app.config import settings
from app.db.schema import (audit_log, cabs, companies, employees, meta,
                           policies, roster)

SEED_FILE = Path(__file__).resolve().parents[2] / "data" / "seed.json"


def make_engine(url: str) -> Engine:
    if not url.startswith("sqlite"):
        return create_engine(url, pool_pre_ping=True)
    eng = create_engine(url, connect_args={"check_same_thread": False})

    # SQLite: wait for locks instead of failing; enforce foreign keys.
    # isolation_level=None stops the driver opening transactions late (after our reads),
    # so WE control BEGIN below.
    @event.listens_for(eng, "connect")
    def _pragmas(conn, _):
        conn.isolation_level = None
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA foreign_keys = ON")

    # Write txns take the write lock at BEGIN, BEFORE counting seats (last-seat race)
    @event.listens_for(eng, "begin")
    def _begin(conn):
        immediate = conn.get_execution_options().get("write_lock", False)
        conn.exec_driver_sql("BEGIN IMMEDIATE" if immediate else "BEGIN")

    return eng


engine = make_engine(settings.database_url)


@contextmanager
def write_txn():
    """One locked write transaction.
    Data Flow: tool -> write_txn -> [SQLite: BEGIN IMMEDIATE | Postgres: BEGIN + FOR UPDATE rows]
               -> reads + writes see a stable view -> COMMIT (or ROLLBACK on any error)."""
    with engine.connect() as conn:
        conn.execution_options(write_lock=True)
        with conn.begin():
            yield conn


def init_db(eng: Engine = None) -> None:
    """Create tables and seed once (only if empty)."""
    eng = eng or engine
    meta.create_all(eng)
    with eng.begin() as conn:
        if conn.execute(select(companies.c.id)).first() is None:
            _seed(conn)


def reset_db(eng: Engine = None) -> None:
    """Drop + recreate + reseed. Seed dates are relative to today, so run this for a fresh demo."""
    eng = eng or engine
    meta.drop_all(eng)
    init_db(eng)


def _seed(conn: Connection) -> None:
    """Data Flow: seed.json -> companies, policies, employees, cabs, roster rows.
    Roster day offsets (0 = today, 1 = tomorrow) become real dates here."""
    data = json.loads(SEED_FILE.read_text())
    seats = {}
    for c in data["companies"]:
        seats[c["id"]] = c["seats"]
        conn.execute(insert(companies).values(id=c["id"], name=c["name"]))
        for trip in ("pickup", "drop"):
            conn.execute(insert(policies).values(
                company_id=c["id"], trip_type=trip, cutoff_minutes=c["cutoff_minutes"]))
    for co, eid, name, role in data["employees"]:
        conn.execute(insert(employees).values(company_id=co, id=eid, name=name, role=role))
    for co, cid, route, trip, at in data["cabs"]:
        conn.execute(insert(cabs).values(
            company_id=co, id=cid, route=route, trip_type=trip, depart_at=at, capacity=seats[co]))
    for co, eid, offset, trip, cab in data["roster"]:
        day = (date.today() + timedelta(days=offset)).isoformat()
        conn.execute(insert(roster).values(
            company_id=co, employee_id=eid, date=day, trip_type=trip, cab_id=cab))


def audit(conn: Connection, company_id: str, actor: str, action: str, **details) -> None:
    """Append one audit row (FR-12): who did what."""
    conn.execute(insert(audit_log).values(
        company_id=company_id, actor=actor, action=action, details=json.dumps(details)))


if __name__ == "__main__":
    # python -m app.db.store reset   (stop the server first)
    # Clears chat checkpoints too: a paused chat must not point at a deleted request.
    import sys
    if sys.argv[1:] == ["reset"]:
        reset_db()
        Path(settings.checkpoint_db).unlink(missing_ok=True)
        print("Mock data re-seeded, chat history cleared.")
    else:
        print("usage: python -m app.db.store reset")
