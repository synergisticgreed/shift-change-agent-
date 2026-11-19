"""Step 5: manager escalation path: pause, persist, resume, and the ugly cases.
Data Flow (tested): /chat (cut-off broken) -> "yes" -> interrupt() -> checkpoint
                    -> /manager/.../decision -> Command(resume) -> DB + employee's chat."""
import threading
import time as time_mod
from datetime import datetime, time, timedelta

from app.agent import booking
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
    # Second click: 404 (already decided) or 409 (first one still running > 1 s lock wait)
    assert codes.count(200) == 1 and set(codes) - {200} <= {404, 409}, codes
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


def test_second_click_while_first_is_slow_gets_409(ctx, monkeypatch):
    rid = _escalate(ctx)
    real, started = booking._decided, threading.Event()

    def slow_decided(*a, **k):  # first approval holds the conversation lock for 1.5 s
        started.set()
        time_mod.sleep(1.5)
        return real(*a, **k)
    monkeypatch.setattr(booking, "_decided", slow_decided)

    first = threading.Thread(target=lambda: _decide(ctx, rid))
    first.start()
    started.wait(2)
    assert _decide(ctx, rid).status_code == 409   # didn't wait past the 1 s lock cap
    first.join()
    assert rq.get_request(ASHA, rid)["status"] == "approved"
