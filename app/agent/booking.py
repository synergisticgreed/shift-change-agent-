"""Booking nodes: decide -> apply, escalate -> wait_manager. No rule logic lives here:
every allow/deny comes from rules.validate (via tools.checks / tools.requests).
Data Flow: Intent -> roster tools (current trip, cab options) -> change dict
           -> checks.precheck -> validate -> allowed: apply_change (re-check in locked txn)
                                          -> denied: reason -> result (+ offer) -> reply"""
from langgraph.types import interrupt

from app.agent.state import identity
from app.llm.budget import new_deadline
from app.rules.engine import nearest_free
from app.tools import checks, roster
from app.tools import requests as rq


# ---------- decide (pre-check, no writes) ----------

def decide(s):
    """Data Flow: Intent -> get_trip + list_options -> change dict -> precheck -> change | denial."""
    i, who = s["intent"], identity(s)
    day, trip = i["date"], i["trip_type"]
    base = {"trip": trip, "date": day}
    try:
        cur = roster.get_trip(who, day, trip)
        if not cur:
            return {"result": {"outcome": "no_booking", **base}, "offer": None}
        options = roster.list_options(who, day, trip)
        ch = {"date": day, "trip_type": trip, "from_cab": cur["cab_id"], "to_cab": None,
              "thread_id": f"{who.company_id}:{who.user_id}"}

        if i["action"] == "change_time":
            target = next((o for o in options if o["depart_at"] == i["new_time"]), None)
            if not target:
                free = ", ".join(o["depart_at"] for o in options if o["booked"] < o["capacity"])
                return {"result": {"outcome": "no_cab", "time": i["new_time"], "options": free, **base},
                        "offer": None}
            if target["id"] == cur["cab_id"]:
                return {"result": {"outcome": "same_time", "time": target["depart_at"], **base},
                        "offer": None}
            ch["to_cab"] = target["id"]

        reason, facts = checks.precheck(who, ch)  # rules decide, not the LLM
    except Exception:
        return {"result": {"outcome": "tool_error"}, "offer": None}
    if reason:
        return deny(who, ch, reason, facts, options)
    return {"change": ch, "offer": None}


def route_decide(s) -> str:
    return "reply" if s.get("result") else "apply"


def deny(who, ch, reason, facts, options=None, full="cab_full"):
    """Data Flow: reason code + facts -> employee-facing result + next option (FR-6)."""
    base = {"trip": ch["trip_type"], "date": ch["date"]}
    if reason == "cab_full":  # offer the nearest cab with a free seat
        options = options or roster.list_options(who, ch["date"], ch["trip_type"])
        return _offer_alternative(options, ch, facts.to_at.strftime("%H:%M"), base, full)
    if reason == "cutoff":    # offer to ask the manager
        m = facts.cutoff_minutes
        earliest = min(t for t in (facts.from_at, facts.to_at) if t)
        return {"result": {"outcome": "cutoff", "cutoff": f"{m // 60}h" if m % 60 == 0 else f"{m} min",
                           "trip_time": earliest.strftime("%H:%M"), **base},
                "offer": {"kind": "escalate", "change": {**ch, "escalate": True, "reason": "after cut-off"}}}
    # stale | trip_passed | duplicate: explain, no offer
    return {"result": {"outcome": reason, **base}, "offer": None}


def _offer_alternative(options, ch, want, base, outcome):
    # Never offer the full cab itself or the cab the employee is already on
    alt = nearest_free([o for o in options if o["id"] not in (ch["to_cab"], ch["from_cab"])], want)
    if not alt:
        return {"result": {"outcome": "no_seats", **base}, "offer": None}
    return {"result": {"outcome": outcome, "time": want, "alt_time": alt["depart_at"], **base},
            "offer": {"kind": "alternative", "change": {**ch, "to_cab": alt["id"]}}}


# ---------- apply (save; rules re-checked under lock) ----------

def apply(s):
    """Data Flow: state["change"] -> requests.apply_change (lock + validate + write) -> result."""
    ch, who = s["change"], identity(s)
    base = {"trip": ch["trip_type"], "date": ch["date"]}
    try:
        rid = rq.apply_change(who, ch, actor=who.user_id)
        if not ch["to_cab"]:
            return {"result": {"outcome": "cancelled", **base}, "request_id": rid, "intent": None}
        now_on = roster.get_trip(who, ch["date"], ch["trip_type"])
    except rq.RuleBroken as e:   # someone was faster, or time ran out
        try:
            return deny(who, ch, str(e), e.facts, full="seat_taken")
        except Exception:
            return {"result": {"outcome": "tool_error"}}
    except Exception:
        return {"result": {"outcome": "tool_error"}}
    return {"result": {"outcome": "applied", "time": now_on["depart_at"], "cab": ch["to_cab"], **base},
            "request_id": rid, "intent": None}  # done -> fresh context


# ---------- escalate + wait for manager (FR-7, FR-8) ----------

def escalate(s):
    """Data Flow: change -> requests.create_pending (DB row the manager can list) -> request id
    -> wait_manager (pause). Separate node so the DB write is checkpointed BEFORE the pause."""
    try:
        rid = rq.create_pending(identity(s), s["change"])
    except Exception:
        return {"result": {"outcome": "tool_error"}}
    return {"request_id": rid, "result": {"outcome": "pending", "request_id": rid}}


def route_escalate(s) -> str:
    return "reply" if s["result"]["outcome"] == "tool_error" else "wait_manager"


def wait_manager(s):
    """The pause point of the whole agent (FR-7, FR-8).
    PAUSE:   interrupt() stops the run here. Nothing below runs yet.
    PERSIST: LangGraph has already saved the state (change, request_id, messages) to the
             checkpoint file under thread_id = "company:employee". The server may now stop.
    RESUME:  manager API -> service.resume -> graph.invoke(Command(resume=decision), thread_id)
             -> LangGraph reloads the state and re-runs THIS node from the top;
                interrupt() now returns the decision instead of pausing.
    Data Flow: decision -> [already decided in DB? report it] -> apply_change | close_request
               -> result -> reply node -> message appended to the employee's chat."""
    d = interrupt({"request_id": s["request_id"]})  # resume value: {"decision", "by", "note"}
    who, ch, rid = identity(s), s["change"], s["request_id"]

    # Idempotent: a replay after a crash (or a 2nd click) must not apply twice
    req = rq.get_request(who, rid)
    if req["status"] != "pending_approval":
        return _decided(who, ch, req)

    if d["decision"] != "approve":
        rq.close_request(who.company_id, rid, "declined", d["by"], d.get("note", ""))
    else:
        try:  # manager overrides cut-off only; seats + departed trips are still enforced
            rq.apply_change(who, ch, actor=d["by"], status="approved", override_cutoff=True,
                            request_id=rid, note=d.get("note", ""))
        except rq.RuleBroken as e:
            rq.close_request(who.company_id, rid, "failed", d["by"], str(e))
    return _decided(who, ch, rq.get_request(who, rid))


def _decided(who, ch, req):
    """Data Flow: final DB status of the request -> employee-facing result (single source of truth)."""
    base = {"trip": ch["trip_type"], "date": ch["date"], "request_id": req["id"]}
    # Resume = new piece of work (maybe hours later): fresh call count + time budget for reply
    done = {"llm_calls": 0, "deadline": new_deadline(), "intent": None}
    if req["status"] == "approved" and not ch["to_cab"]:
        return {"result": {"outcome": "approved_cancel", **base}, **done}
    if req["status"] == "approved":
        now_on = roster.get_trip(who, ch["date"], ch["trip_type"])
        return {"result": {"outcome": "approved", "time": now_on["depart_at"], **base}, **done}
    return {"result": {"outcome": req["status"], **base}, **done}   # declined | failed
