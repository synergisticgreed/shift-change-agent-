"""Agent workflow when the LLM misbehaves: bad JSON, outages, fact changes, budget.
Data Flow (tested): message -> understand (scripted LLM) -> validate/retry/fallback
                    -> code decides -> reply (scripted LLM) -> fact guard -> employee."""
import json

import httpx
import pytest

from app.agent import service
from app.agent.graph import build_graph
from app.llm.client import GroqLLM, LLMError
from tests.conftest import TOMORROW, booked_cab

GOOD = json.dumps({"action": "change_time", "trip_type": "drop", "date": TOMORROW, "new_time": "19:00"})


class ScriptedLLM:
    """Returns the next scripted answer; an Exception in the script is raised instead."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), 0

    def complete(self, system, user, json_mode=False):
        self.calls += 1
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a


def _use(ctx, monkeypatch, llm):
    monkeypatch.setattr(service, "graph", build_graph(llm, ctx.checkpoint))
    return llm


def test_bad_json_is_retried_once(ctx, monkeypatch):
    llm = _use(ctx, monkeypatch, ScriptedLLM("not json", GOOD, "Sorted! Drop on " + TOMORROW + " at 19:00."))
    r = ctx.chat("acme", "E101", "move my drop")
    assert r["status"] == "applied" and llm.calls == 3          # 2 understand + 1 reply
    assert booked_cab("acme", "E101") == "A-D2"


def test_bad_json_twice_asks_instead_of_guessing(ctx, monkeypatch):
    llm = _use(ctx, monkeypatch, ScriptedLLM("{oops", '{"action": "teleport"}'))
    r = ctx.chat("acme", "E101", "move my drop to 7")
    assert r["status"] == "unclear" and llm.calls == 2          # question is templated
    assert booked_cab("acme", "E101") == "A-D1"


def test_llm_down_code_still_handles_request(ctx, monkeypatch):
    llm = _use(ctx, monkeypatch, ScriptedLLM(LLMError("timeout"), LLMError("timeout")))
    r = ctx.chat("acme", "E101", "Move my drop tomorrow to 19:00")
    assert r["status"] == "applied" and "19:00" in r["reply"]   # keyword parser + template
    assert llm.calls == 2


def test_reply_cannot_change_facts(ctx, monkeypatch):
    _use(ctx, monkeypatch, ScriptedLLM(GOOD, "Done! Your drop is now at 21:00."))  # wrong time
    r = ctx.chat("acme", "E101", "move my drop")
    assert "19:00" in r["reply"] and "21:00" not in r["reply"]  # template used instead


def test_llm_cannot_pick_another_company(ctx, monkeypatch):
    # LLM output has no company/employee field; extra keys are ignored, identity stays the token's
    evil = json.dumps({**json.loads(GOOD), "company_id": "globex", "employee_id": "G201"})
    _use(ctx, monkeypatch, ScriptedLLM(evil, "x"))
    ctx.chat("acme", "E101", "move my drop")
    assert booked_cab("acme", "E101") == "A-D2" and booked_cab("globex", "G201") == "G-D1"


@pytest.mark.parametrize("fake_post", [
    lambda *a, **k: (_ for _ in ()).throw(httpx.ReadTimeout("slow")),
    lambda *a, **k: httpx.Response(500, request=httpx.Request("POST", GroqLLM.URL)),
    lambda *a, **k: httpx.Response(200, json={"weird": True}, request=httpx.Request("POST", GroqLLM.URL)),
])
def test_groq_failures_become_llm_error(monkeypatch, fake_post):
    monkeypatch.setattr(httpx, "post", fake_post)
    with pytest.raises(LLMError):
        GroqLLM().complete("sys", "user")


def test_groq_happy_path(monkeypatch):
    ok = {"choices": [{"message": {"content": "hi"}}]}
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(
        200, json=ok, request=httpx.Request("POST", GroqLLM.URL)))
    assert GroqLLM().complete("sys", "user") == "hi"
