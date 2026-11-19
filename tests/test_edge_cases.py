"""Regression tests for bugs found in the Step 1 review.
Data Flow (each test): HTTP /chat or /manager -> agent -> DB -> assert reply + DB state."""
from datetime import datetime, time, timedelta

from sqlalchemy import insert

from app.agent.nodes import _same_facts
from app.db import store
from app.db.schema import roster
from tests.conftest import TODAY, TOMORROW, auth, booked_cab

LATE = datetime.combine(TODAY + timedelta(days=1), time(17, 0))  # 1h before the 18:00 drop


def test_same_time_is_a_no_op(ctx):
    r = ctx.chat("acme", "E101", "Move my drop tomorrow to 18:00")  # already on 18:00
    assert r["status"] == "same_time" and r["request_id"] is None


def test_repeat_change_is_not_a_duplicate(ctx):
    for t, cab in [("19:00", "A-D2"), ("18:00", "A-D1"), ("19:00", "A-D2")]:
        assert ctx.chat("acme", "E101", f"Move my drop tomorrow to {t}")["status"] == "applied"
        assert booked_cab("acme", "E101") == cab


def test_manager_approves_late_cancel(ctx):
    ctx.set_now(LATE)
    assert ctx.chat("acme", "E101", "Cancel my drop today")["status"] == "cutoff"
    rid = ctx.chat("acme", "E101", "yes")["request_id"]
    res = ctx.client.post(f"/manager/requests/{rid}/decision", json={"decision": "approve"},
                          headers=auth("acme", "M1", "manager"))
    assert res.status_code == 200 and res.json()["status"] == "approved"
    assert booked_cab("acme", "E101") is None


def test_alternative_is_never_your_current_cab(ctx):
    # Fill 19:00 (A-D2) and 20:00 (A-D3); 20:30 is full from seed.
    # Only free cab left is E101's own 18:00 -> must say "no seats", not offer it.
    with store.engine.begin() as conn:
        for i, cab in enumerate(["A-D2"] * 4 + ["A-D3"]):
            conn.execute(insert(roster).values(company_id="acme", employee_id=f"X{i}",
                                               date=TOMORROW, trip_type="drop", cab_id=cab))
    r = ctx.chat("acme", "E101", "Move my drop tomorrow to 20:00")
    assert r["status"] == "no_seats"


def test_stale_offer_is_dropped(ctx):
    ctx.chat("acme", "E101", "Move my drop tomorrow to 20:30")   # offer: 20:00
    ctx.chat("acme", "E101", "any update?")                      # unrelated message
    assert ctx.chat("acme", "E101", "yes")["status"] == "unclear"
    assert booked_cab("acme", "E101") == "A-D1"


def test_resume_needs_a_paused_request(ctx):
    rid = ctx.chat("acme", "E101", "Move my drop tomorrow to 19:00")["request_id"]  # applied
    res = ctx.client.post(f"/manager/requests/{rid}/decision", json={"decision": "approve"},
                          headers=auth("acme", "M1", "manager"))
    assert res.status_code == 404


def test_reply_guard_checks_dates():
    draft = "Done! Your drop on 2026-11-21 is now at 19:00 (cab A-D2)."
    assert not _same_facts(draft, "Done! Your drop on 2026-11-22 is now at 19:00.")
    assert _same_facts(draft, "All set: drop on 2026-11-21 at 19:00.")
