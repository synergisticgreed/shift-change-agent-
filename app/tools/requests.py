"""Write tools: apply a change, raise/decide a request, read requests.
Rules are re-checked inside the save transaction (rules checked twice).
Data Flow: agent node (change dict + Identity) -> one locked DB transaction
           [load_facts -> validate -> roster -> shift_requests -> audit_log] -> request id."""
import hashlib

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from app.auth.jwt import Identity
from app.db import store
from app.db.schema import roster, shift_requests
from app.rules.validate import validate
from app.tools.checks import load_facts


class RuleBroken(Exception):
    """A rule failed at save time. str(e) = reason code; e.facts = what the rules saw."""

    def __init__(self, reason: str, facts=None):
        super().__init__(reason)
        self.facts = facts


def idem_key(who: Identity, ch: dict, seq: int) -> str:
    """Same change + same seq -> same key -> UNIQUE blocks the copy (bonus).
    seq = requests already made for this trip, so a double submit (same seq) is blocked
    but a later, deliberate repeat (higher seq) is allowed."""
    raw = (f"{who.company_id}|{who.user_id}|{ch['date']}|{ch['trip_type']}"
           f"|{ch['from_cab']}|{ch['to_cab']}|{seq}")
    return hashlib.sha256(raw.encode()).hexdigest()


def apply_change(who: Identity, ch: dict, actor: str, status: str = "applied",
                 override_cutoff: bool = False, request_id: int | None = None, note: str = "") -> int:
    """Move/cancel the employee's trip in ONE locked transaction. Returns request id.
    ch = {date, trip_type, from_cab, to_cab (None=cancel), thread_id, reason}
    Data Flow: write_txn (lock) -> [approval: still pending?] -> load_facts -> validate
               -> [fail: RuleBroken, rollback] -> roster -> shift_requests -> audit_log -> COMMIT"""
    co, emp, day = who.company_id, who.user_id, ch["date"]
    with store.write_txn() as conn:
        # Approvals only act on a request that is STILL pending (double click / crash replay)
        if request_id and not _is_pending(conn, co, request_id):
            raise RuleBroken("already_decided")
        # Re-check every rule with fresh, locked data (the pre-check may be stale by now)
        facts = load_facts(conn, who, ch, lock=True)
        reason = validate(facts, override_cutoff)
        if reason:
            raise RuleBroken(reason, facts)

        # Move the trip: drop old roster row, add new one
        conn.execute(delete(roster).where(
            roster.c.company_id == co, roster.c.employee_id == emp,
            roster.c.date == day, roster.c.trip_type == ch["trip_type"]))
        if ch["to_cab"]:
            conn.execute(insert(roster).values(company_id=co, employee_id=emp, date=day,
                                               trip_type=ch["trip_type"], cab_id=ch["to_cab"]))

        if request_id:  # manager-approved pending request (company filter = isolation)
            conn.execute(update(shift_requests).where(
                shift_requests.c.id == request_id, shift_requests.c.company_id == co).values(
                status=status, decided_by=actor, decided_at=func.now()))
        else:
            request_id = _insert_request(conn, who, ch, status)
        store.audit(conn, co, actor, f"request_{status}", request_id=request_id, note=note, **_brief(ch))
    return request_id


def create_pending(who: Identity, ch: dict) -> int:
    """Escalate: store a pending_approval request (idempotent on retry/resume)."""
    with store.write_txn() as conn:
        found = conn.execute(select(shift_requests.c.id).where(
            *_same_trip(who, ch), shift_requests.c.from_cab.is_not_distinct_from(ch["from_cab"]),
            shift_requests.c.to_cab.is_not_distinct_from(ch["to_cab"]),
            shift_requests.c.status == "pending_approval")).scalar()
        if found:
            return found
        rid = _insert_request(conn, who, ch, "pending_approval")
        store.audit(conn, who.company_id, who.user_id, "request_escalated", request_id=rid, **_brief(ch))
    return rid


def close_request(company_id: str, request_id: int, status: str, actor: str, note: str = "") -> bool:
    """End a PENDING request without applying it (declined / failed). False = already decided."""
    with store.write_txn() as conn:
        done = conn.execute(update(shift_requests).where(
            shift_requests.c.id == request_id, shift_requests.c.company_id == company_id,
            shift_requests.c.status == "pending_approval")   # never overwrite a decision
            .values(status=status, decided_by=actor, decided_at=func.now())).rowcount
        if done:
            store.audit(conn, company_id, actor, f"request_{status}", request_id=request_id, note=note)
    return bool(done)


def get_request(who: Identity, request_id: int) -> dict | None:
    """Employees see only their own; managers see their company's."""
    q = select(shift_requests).where(shift_requests.c.id == request_id,
                                     shift_requests.c.company_id == who.company_id)
    if who.role != "manager":
        q = q.where(shift_requests.c.employee_id == who.user_id)
    with store.engine.connect() as conn:
        row = conn.execute(q).mappings().first()
    return dict(row) if row else None


def latest_request(who: Identity) -> dict | None:
    q = (select(shift_requests).where(shift_requests.c.company_id == who.company_id,
                                      shift_requests.c.employee_id == who.user_id)
         .order_by(shift_requests.c.id.desc()).limit(1))
    with store.engine.connect() as conn:
        row = conn.execute(q).mappings().first()
    return dict(row) if row else None


def list_pending(manager: Identity) -> list[dict]:
    q = select(shift_requests).where(shift_requests.c.company_id == manager.company_id,
                                     shift_requests.c.status == "pending_approval")
    with store.engine.connect() as conn:
        return [dict(r) for r in conn.execute(q).mappings()]


# --- helpers ---

def _is_pending(conn, company_id: str, request_id: int) -> bool:
    return conn.execute(select(shift_requests.c.status).where(
        shift_requests.c.id == request_id, shift_requests.c.company_id == company_id)
    ).scalar() == "pending_approval"


def _same_trip(who: Identity, ch: dict) -> tuple:
    """WHERE parts: this employee's requests for this date + trip."""
    r = shift_requests.c
    return (r.company_id == who.company_id, r.employee_id == who.user_id,
            r.date == ch["date"], r.trip_type == ch["trip_type"])


def _insert_request(conn, who: Identity, ch: dict, status: str) -> int:
    seq = conn.execute(select(func.count()).select_from(shift_requests)
                       .where(*_same_trip(who, ch))).scalar_one()
    try:
        with conn.begin_nested():  # savepoint, so a duplicate doesn't kill the txn
            return conn.execute(insert(shift_requests).values(
                company_id=who.company_id, employee_id=who.user_id, date=ch["date"],
                trip_type=ch["trip_type"], from_cab=ch["from_cab"], to_cab=ch["to_cab"],
                status=status, reason=ch.get("reason"), thread_id=ch["thread_id"],
                idempotency_key=idem_key(who, ch, seq))).inserted_primary_key[0]
    except IntegrityError:
        raise RuleBroken("duplicate")


def _brief(ch: dict) -> dict:
    return {k: ch.get(k) for k in ("date", "trip_type", "from_cab", "to_cab")}
