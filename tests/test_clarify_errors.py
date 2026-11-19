"""Step 7: clarifying questions (FR-9) + plain errors, never a stack trace (FR-11).
Data Flow (tested): unclear text -> clarify question -> short answer -> merged intent -> action
                    tool/node crash -> errors.tool_error (logged) -> plain reply; chat still usable."""
import logging

from fastapi.testclient import TestClient

from app.agent import booking, service
from app.agent.graph import build_graph
from app.llm.fake import FakeLLM
from app.main import app
from app.tools import requests as rq
from tests.conftest import auth, booked_cab


# ---------- clarifying questions ----------

def test_step_by_step_answers_complete_the_request(ctx):
    assert "pickup or your drop" in ctx.chat("acme", "E101", "I want to change my cab time")["reply"]
    assert "Which day" in ctx.chat("acme", "E101", "drop")["reply"]
    assert "What time" in ctx.chat("acme", "E101", "tomorrow")["reply"]
    r = ctx.chat("acme", "E101", "7 pm")
    assert r["status"] == "applied" and booked_cab("acme", "E101") == "A-D2"


def test_time_question_lists_free_cabs(ctx):
    r = ctx.chat("acme", "E101", "Can I leave a bit late tomorrow?")
    assert r["status"] == "clarify"
    assert "19:00" in r["reply"] and "20:00" in r["reply"]
    assert "20:30" not in r["reply"]          # full cab is not offered
    assert "18:00" not in r["reply"]          # current cab is not offered


def test_both_trips_in_one_message_asks_which(ctx):
    r = ctx.chat("acme", "E101", "move my pickup and drop tomorrow to 9")
    assert r["status"] == "clarify" and "pickup or your drop" in r["reply"]


# ---------- errors: plain messages, logged traces, no wedged chats ----------

def test_crashed_step_does_not_lock_the_conversation(ctx, monkeypatch, caplog):
    def boom(s):
        raise RuntimeError("bug in decide")
    with monkeypatch.context() as m:
        m.setattr(booking, "decide", boom)
        m.setattr(service, "graph", build_graph(FakeLLM(), ctx.checkpoint))
        with caplog.at_level(logging.ERROR):
            r = ctx.chat("acme", "E101", "Move my drop tomorrow to 19:00")
    assert r["status"] == "failed" and "Traceback" not in r["reply"]
    assert any(rec.exc_info for rec in caplog.records)          # full trace went to the log
    # Same conversation, real code again: works normally (not stuck as "paused")
    assert ctx.chat("acme", "E101", "Move my drop tomorrow to 19:00")["status"] == "applied"


def test_tool_failure_is_logged_not_shown(ctx, monkeypatch, caplog):
    def down(*a, **k):
        raise ConnectionError("db down")
    monkeypatch.setattr(rq, "latest_request", down)
    with caplog.at_level(logging.ERROR):
        r = ctx.chat("acme", "E101", "any update?")
    assert "try again" in r["reply"] and "db down" not in r["reply"]
    assert any("db down" in str(rec.exc_info) for rec in caplog.records if rec.exc_info)


def test_bad_input_gets_plain_422(ctx):
    for body in ({"message": ""}, {"message": "x" * 501}, {"msg": "hi"}):
        res = ctx.client.post("/chat", json=body, headers=auth("acme", "E101"))
        assert res.status_code == 422 and isinstance(res.json()["detail"], str)
    res = ctx.client.post("/chat", content="not json", headers={**auth("acme", "E101"),
                                                                 "Content-Type": "application/json"})
    assert res.status_code == 422 and isinstance(res.json()["detail"], str)


def test_unexpected_crash_is_plain_500(ctx, monkeypatch):
    def boom(*a, **k):
        raise KeyError("secret internals")
    monkeypatch.setattr(rq, "get_request", boom)
    client = TestClient(app, raise_server_exceptions=False)
    res = client.get("/requests/1", headers=auth("acme", "E101"))
    assert res.status_code == 500
    assert res.json() == {"detail": "Something went wrong. Please try again."}
