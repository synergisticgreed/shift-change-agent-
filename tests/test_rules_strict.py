"""Step 4: strict rules in code + cross-company isolation.
Data Flow (tested): change -> tools.checks.load_check (DB) -> rules.validate -> allow / reason."""
import threading
from datetime import datetime, time, timedelta

import pytest
from sqlalchemy import insert
from sqlalchemy.exc import NoResultFound

from app.agent import nodes
from app.auth.jwt import Identity
from app.db import store
from app.db.schema import roster as roster_t
from app.rules.validate import Facts, validate
from app.tools import checks, roster
from app.tools import requests as rq
from tests.conftest import TODAY, TOMORROW, auth, booked_cab

ASHA, ELI = Identity("acme", "E101", "employee"), Identity("acme", "E105", "employee")


def _change(from_cab, to_cab, who=ASHA):
    return {"date": TOMORROW, "trip_type": "drop", "from_cab": from_cab, "to_cab": to_cab,
            "thread_id": f"{who.company_id}:{who.user_id}"}


def test_last_seat_race_exactly_one_wins(ctx):
    # A-D3 (20:00) has 3/4 booked. Asha and Eli grab the last seat at the same moment.
    results, gate = {}, threading.Barrier(2)

    def grab(who, from_cab):
        gate.wait()
        try:
            rq.apply_change(who, _change(from_cab, "A-D3", who), actor=who.user_id)
            results[who.user_id] = "ok"
        except rq.RuleBroken as e:
            results[who.user_id] = str(e)
        except Exception as e:  # anything else = bug (e.g. "database is locked")
            results[who.user_id] = f"crash: {type(e).__name__}"

    threads = [threading.Thread(target=grab, args=a) for a in ((ASHA, "A-D1"), (ELI, "A-D4"))]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results.values()) == ["cab_full", "ok"], results


# ---------- rules/validate.py (pure) ----------

def _facts(**kw):
    now = datetime(2026, 1, 1, 12, 0)
    base = dict(now=now, cutoff_minutes=120, booked_cab="A", from_cab="A",
                from_at=now + timedelta(hours=6), to_at=now + timedelta(hours=7), capacity=4, booked=0)
    return Facts(**{**base, **kw})


def test_validate_rule_order_and_override():
    now = datetime(2026, 1, 1, 12, 0)
    assert validate(_facts()) is None
    assert validate(_facts(booked_cab="B")) == "stale"
    assert validate(_facts(from_at=now - timedelta(minutes=1))) == "trip_passed"
    assert validate(_facts(booked=4)) == "cab_full"
    assert validate(_facts(to_at=now + timedelta(hours=1))) == "cutoff"   # NEW time too close
    # Full AND inside cut-off -> seats reported first (no pointless escalation)
    assert validate(_facts(booked=4, to_at=now + timedelta(hours=1))) == "cab_full"
    # Manager override skips cut-off only
    assert validate(_facts(to_at=now + timedelta(hours=1)), override_cutoff=True) is None
    assert validate(_facts(booked=4), override_cutoff=True) == "cab_full"
    assert validate(_facts(from_at=now - timedelta(minutes=1)), override_cutoff=True) == "trip_passed"


def test_rules_are_not_in_llm_prompts():
    for prompt in (nodes.UNDERSTAND_PROMPT, nodes.REPLY_PROMPT):
        assert not any(w in prompt.lower() for w in ("cut-off", "cutoff", "seat", "capacity", "hour"))


# ---------- cut-off through the agent ----------

def _at(h, m=0, day=1):
    return datetime.combine(TODAY + timedelta(days=day), time(h, m))


def test_each_company_uses_its_own_cutoff(ctx):
    # Same 3.5h gap before the drop: Acme (2h rule) allows, Globex (4h rule) blocks
    ctx.set_now(_at(14, 30))   # Acme drop 18:00 -> 3.5h
    assert ctx.chat("acme", "E101", "Move my drop today to 19:00")["status"] == "applied"
    ctx.set_now(_at(13, 30))   # Globex drop 17:00 -> 3.5h
    r = ctx.chat("globex", "G201", "Move my drop today to 19:00")
    assert r["status"] == "cutoff" and "4h before" in r["reply"]


def test_cutoff_checks_the_new_time_too(ctx):
    assert ctx.chat("acme", "E101", "Move my pickup tomorrow to 9 am")["status"] == "applied"
    ctx.set_now(_at(6, 30))    # old pickup 09:00 is 2.5h away, new 08:00 only 1.5h
    assert ctx.chat("acme", "E101", "Move my pickup today to 8 am")["status"] == "cutoff"
    assert booked_cab("acme", "E101", "pickup") == "A-P2"


def test_departed_trip_cannot_be_changed_or_escalated(ctx):
    ctx.set_now(_at(18, 30))   # 18:00 drop already left
    assert ctx.chat("acme", "E101", "Move my drop today to 20:00")["status"] == "trip_passed"
    assert ctx.chat("acme", "E101", "yes")["status"] == "unclear"   # no escalation offer
    assert booked_cab("acme", "E101") == "A-D1"


def test_manager_cannot_approve_after_departure(ctx):
    ctx.set_now(_at(17, 0))
    ctx.chat("acme", "E101", "Move my drop today to 19:00")
    rid = ctx.chat("acme", "E101", "yes")["request_id"]
    ctx.set_now(_at(18, 30))   # manager is late; the 18:00 drop has left
    res = ctx.client.post(f"/manager/requests/{rid}/decision", json={"decision": "approve"},
                          headers=_mgr())
    assert res.json()["status"] == "failed" and "already left" in res.json()["employee_reply"]
    assert booked_cab("acme", "E101") == "A-D1"


def test_manager_cannot_approve_into_a_full_cab(ctx):
    ctx.set_now(_at(17, 0))
    ctx.chat("acme", "E101", "Move my drop today to 19:00")
    rid = ctx.chat("acme", "E101", "yes")["request_id"]
    _fill("A-D2", 4)           # 19:00 fills up while the manager is deciding
    res = ctx.client.post(f"/manager/requests/{rid}/decision", json={"decision": "approve"},
                          headers=_mgr())
    assert res.json()["status"] == "failed"
    assert booked_cab("acme", "E101") == "A-D1"


# ---------- isolation at the tool level ----------

IVAN = Identity("globex", "G201", "employee")


def test_tools_never_cross_companies(ctx):
    rid = ctx.chat("acme", "E101", "Move my drop tomorrow to 19:00")["request_id"]
    # Read tools: Globex identity sees only Globex rows
    assert {o["id"][0] for o in roster.list_options(IVAN, TOMORROW, "drop")} == {"G"}
    assert rq.get_request(IVAN, rid) is None
    assert rq.get_request(Identity("globex", "M2", "manager"), rid) is None
    assert rq.list_pending(Identity("globex", "M2", "manager")) == []
    # Write/check tools: pointing at an Acme cab with a Globex identity fails, writes nothing
    bad = _change("G-D1", "A-D2", IVAN)
    with pytest.raises(NoResultFound):  # Acme cab is invisible to Globex
        checks.precheck(IVAN, bad)
    with pytest.raises(NoResultFound):  # Acme cab is invisible to Globex
        rq.apply_change(IVAN, bad, actor="G201")
    assert booked_cab("globex", "G201") == "G-D1"


def test_manager_decision_cannot_touch_other_company(ctx):
    ctx.set_now(_at(17, 0))
    ctx.chat("acme", "E101", "Move my drop today to 19:00")
    rid = ctx.chat("acme", "E101", "yes")["request_id"]
    res = ctx.client.post(f"/manager/requests/{rid}/decision", json={"decision": "reject"},
                          headers=auth("globex", "M2", "manager"))
    assert res.status_code == 404
    assert rq.get_request(Identity("acme", "M1", "manager"), rid)["status"] == "pending_approval"


# ---------- helpers ----------

def _mgr():
    return auth("acme", "M1", "manager")


def _fill(cab_id, n):
    """Put n extra riders on a cab for the trip date the test is using (today+1)."""
    with store.engine.begin() as conn:
        for k in range(n):
            conn.execute(insert(roster_t).values(company_id="acme", employee_id=f"X{cab_id}{k}",
                                               date=TOMORROW, trip_type="drop", cab_id=cab_id))
