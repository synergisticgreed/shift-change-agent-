"""Acceptance criteria from spec.md §6, end to end through the API."""
from datetime import datetime, time, timedelta

import pytest

from app.agent import service
from app.agent.graph import build_graph
from app.llm.fake import FakeLLM
from app.tools import roster
from tests.conftest import TODAY, TOMORROW, auth, booked_cab


def test_happy_path_applies_change(ctx):
    r = ctx.chat("acme", "E101", "Move my drop tomorrow to 19:00")
    assert r["status"] == "applied" and "19:00" in r["reply"]
    assert booked_cab("acme", "E101") == "A-D2"


def test_cross_company_isolation(ctx):
    # Acme employee creates a request
    rid = ctx.chat("acme", "E101", "Move my drop tomorrow to 19:00")["request_id"]
    # Globex employee / manager cannot see or decide it
    assert ctx.client.get(f"/requests/{rid}", headers=auth("globex", "G201")).status_code == 404
    assert ctx.client.get("/manager/pending", headers=auth("globex", "M2", "manager")).json() == []
    # Globex only ever sees its own cabs (20:00 is an Acme time)
    r = ctx.chat("globex", "G201", "Move my drop tomorrow to 20:00")
    assert r["status"] == "no_cab" and "17:00, 19:00" in r["reply"]


def test_cutoff_rejected_with_reason_and_option(ctx):
    ctx.set_now(datetime.combine(TODAY + timedelta(days=1), time(17, 0)))  # 1h before 18:00
    r = ctx.chat("acme", "E101", "Move my drop today to 19:00")
    assert r["status"] == "cutoff"
    assert "2h before" in r["reply"] and "manager" in r["reply"]  # reason + next option
    assert booked_cab("acme", "E101") == "A-D1"                    # nothing changed


def test_escalation_resumes_after_restart(ctx):
    ctx.set_now(datetime.combine(TODAY + timedelta(days=1), time(17, 0)))
    ctx.chat("acme", "E101", "Move my drop today to 19:00")
    r = ctx.chat("acme", "E101", "yes")
    assert r["status"] == "pending_approval"
    rid = r["request_id"]

    # While paused, new messages just report the pending request
    assert ctx.chat("acme", "E101", "hello?")["status"] == "pending_approval"

    # "Restart": brand-new graph object reading the same checkpoint file
    service.graph = build_graph(FakeLLM(), ctx.checkpoint)

    res = ctx.client.post(f"/manager/requests/{rid}/decision", json={"decision": "approve"},
                          headers=auth("acme", "M1", "manager"))
    assert res.status_code == 200 and res.json()["status"] == "approved"
    assert booked_cab("acme", "E101") == "A-D2"
    # Audit: who asked and who decided
    req = ctx.client.get(f"/requests/{rid}", headers=auth("acme", "E101")).json()
    assert req["employee_id"] == "E101" and req["decided_by"] == "M1"


def test_manager_reject_keeps_trip(ctx):
    ctx.set_now(datetime.combine(TODAY + timedelta(days=1), time(17, 0)))
    ctx.chat("acme", "E101", "Move my drop today to 19:00")
    rid = ctx.chat("acme", "E101", "yes")["request_id"]
    res = ctx.client.post(f"/manager/requests/{rid}/decision", json={"decision": "reject"},
                          headers=auth("acme", "M1", "manager"))
    assert res.json()["status"] == "declined"
    assert booked_cab("acme", "E101") == "A-D1"


@pytest.mark.parametrize("msg, status", [
    ("Move my drop tomorrow to 19:00", "applied"),   # change -> apply tool
    ("Cancel my pickup tomorrow", "applied"),        # cancel -> apply tool (to_cab=None)
    ("Any update on my request?", "no_requests"),    # status -> read tool only
    ("Move my drop tomorrow to 20:30", "cab_full"),  # full cab -> offer, no write
])
def test_right_tool_for_each_request(ctx, msg, status):
    assert ctx.chat("acme", "E101", msg)["status"] == status


def test_full_cab_offers_nearest_then_yes_books_it(ctx):
    r = ctx.chat("acme", "E101", "Move my drop tomorrow to 20:30")
    assert "20:00" in r["reply"]                      # nearest free slot offered
    assert ctx.chat("acme", "E101", "yes")["status"] == "applied"
    assert booked_cab("acme", "E101") == "A-D3"


def test_tool_timeout_is_friendly_and_changes_nothing(ctx, monkeypatch):
    def boom(*a, **k):
        raise TimeoutError("roster db slow")
    with monkeypatch.context() as m:  # patch only inside this block
        m.setattr(roster, "get_trip", boom)
        r = ctx.chat("acme", "E101", "Move my drop tomorrow to 19:00")
    assert r["status"] == "tool_error" and "Nothing was changed" in r["reply"]
    assert booked_cab("acme", "E101") == "A-D1"


def test_ambiguous_request_gets_a_question(ctx):
    r = ctx.chat("acme", "E101", "Can I leave a bit late tomorrow?")
    assert r["status"] == "clarify" and r["reply"].endswith("?")
    assert booked_cab("acme", "E101") == "A-D1"


def test_keeps_context_across_messages(ctx):
    ctx.chat("acme", "E101", "Can I leave a bit late tomorrow?")  # asks for a time
    r = ctx.chat("acme", "E101", "make it 7 instead")             # 7 -> 19:00 drop
    assert r["status"] == "applied"
    assert booked_cab("acme", "E101") == "A-D2"


def test_llm_budget_per_message(ctx):
    before = ctx.fake.calls
    ctx.chat("acme", "E101", "Move my drop tomorrow to 19:00")
    assert ctx.fake.calls - before <= 4


def test_bad_token_is_401(ctx):
    res = ctx.client.post("/chat", json={"message": "hi"}, headers={"Authorization": "Bearer x"})
    assert res.status_code == 401 and res.json()["detail"] == "Please log in again."
