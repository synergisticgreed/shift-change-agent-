"""Step 5: manager escalation path: pause, persist, resume, and the ugly cases.
Data Flow (tested): /chat (cut-off broken) -> "yes" -> interrupt() -> checkpoint
                    -> /manager/.../decision -> Command(resume) -> DB + employee's chat."""
import threading
from datetime import datetime, time, timedelta

from app.auth.jwt import Identity
from app.tools import requests as rq
from tests.conftest import TODAY, auth, booked_cab

MGR = auth("acme", "M1", "manager")
ASHA = Identity("acme", "E101", "employee")


def _escalate(ctx) -> int:
    """Asha asks inside the cut-off and says yes -> pending request id."""
    ctx.set_now(datetime.combine(TODAY + timedelta(days=1), time(17, 0)))
    ctx.chat("acme", "E101", "Move my drop today to 19:00")
    r = ctx.chat("acme", "E101", "yes")
    assert r["status"] == "pending_approval"
    return r["request_id"]


def _decide(ctx, rid, decision="approve"):
    return ctx.client.post(f"/manager/requests/{rid}/decision", json={"decision": decision}, headers=MGR)


def test_double_click_approve_applies_once(ctx):
    rid = _escalate(ctx)
    codes, gate = [], threading.Barrier(2)

    def click():
        gate.wait()
        codes.append(_decide(ctx, rid).status_code)

    threads = [threading.Thread(target=click) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(codes) == [200, 404]                      # second click: nothing pending
    assert rq.get_request(ASHA, rid)["status"] == "approved"  # not overwritten to failed
    assert booked_cab("acme", "E101") == "A-D2"


def test_crash_after_db_commit_heals_on_next_message(ctx):
    rid = _escalate(ctx)
    # Simulate: wait_manager committed the approval, then the server died
    # before the checkpoint was saved -> thread still paused, DB says approved.
    req = rq.get_request(ASHA, rid)
    change = {k: req[k] for k in ("date", "trip_type", "from_cab", "to_cab", "thread_id")}
    rq.apply_change(ASHA, change, actor="M1", status="approved", override_cutoff=True, request_id=rid)

    # Employee writes again: must hear the real outcome, not "still pending" forever
    r = ctx.chat("acme", "E101", "any news?")
    assert r["status"] == "approved" and f"#{rid}" in r["reply"]
    # And the conversation is usable again afterwards
    assert ctx.chat("acme", "E101", "any update?")["status"] == "status"


def test_decision_only_applies_to_pending(ctx):
    rid = _escalate(ctx)
    assert _decide(ctx, rid, "reject").json()["status"] == "declined"
    assert _decide(ctx, rid, "approve").status_code == 404   # can't flip a decided request
    assert booked_cab("acme", "E101") == "A-D1"
