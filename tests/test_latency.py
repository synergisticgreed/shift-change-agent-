"""NFR-1 (p95 < 5 s) and NFR-2 (<= 4 LLM calls) under slow, broken and hanging LLMs.
Data Flow (tested): /chat -> deadline stamped -> budget.ask caps each call
                    -> fallback to code when time is up -> reply within 5 s."""
import json
import threading
import time

import httpx
import pytest

from app.agent import service
from app.agent.graph import build_graph, thread_id
from app.llm import budget
from app.llm.client import GroqLLM, LLMError
from tests.conftest import TOMORROW, booked_cab

LIMIT_S = 5.0
GOOD = json.dumps({"action": "change_time", "trip_type": "drop", "date": TOMORROW, "new_time": "19:00"})


class SlowLLM:
    """Acts like GroqLLM: takes `delay` seconds, but never longer than the timeout it is given."""

    def __init__(self, delay, answers):
        self.delay, self.answers, self.calls = delay, list(answers), 0

    def complete(self, system, user, json_mode=False, timeout=None):
        self.calls += 1
        time.sleep(min(self.delay, timeout))
        if self.delay > timeout:
            raise LLMError("timeout")
        return self.answers.pop(0) if self.answers else json.loads(user).get("draft", "")


def _timed_chat(ctx, monkeypatch, llm, msg="Move my drop tomorrow to 19:00"):
    monkeypatch.setattr(service, "graph", build_graph(llm, ctx.checkpoint))
    t = time.perf_counter()
    r = ctx.chat("acme", "E101", msg)
    return r, time.perf_counter() - t


def test_slow_llm_every_call_still_under_5s(ctx, monkeypatch):
    llm = SlowLLM(1.4, [GOOD])                       # just under the 1.5 s per-call cap
    r, took = _timed_chat(ctx, monkeypatch, llm)
    assert r["status"] == "applied" and took < LIMIT_S
    assert r["llm_calls"] == llm.calls == 2


def test_slow_llm_with_retry_uses_budget_then_template(ctx, monkeypatch):
    llm = SlowLLM(1.4, ["not json", GOOD])           # 1.4 + 1.4 = 2.8 s; reply gets only 0.7 s
    r, took = _timed_chat(ctx, monkeypatch, llm)
    assert r["status"] == "applied" and "19:00" in r["reply"]
    # 3 calls normally; on a busy machine < 0.3 s may be left, so the reply call is skipped (2)
    assert took < LIMIT_S and r["llm_calls"] == llm.calls and llm.calls in (2, 3)


def test_hanging_llm_falls_back_to_code(ctx, monkeypatch):
    llm = SlowLLM(30, [])                            # never answers
    r, took = _timed_chat(ctx, monkeypatch, llm)
    assert r["status"] == "applied" and booked_cab("acme", "E101") == "A-D2"
    assert took < LIMIT_S and llm.calls <= 4


def test_budget_refuses_before_calling(monkeypatch):
    class Boom:
        def complete(self, *a, **k):
            raise AssertionError("LLM must not be called")
    future = {"deadline": time.time() + 10}
    with pytest.raises(budget.BudgetExceeded):
        budget.ask(Boom(), future, budget.MAX_LLM_CALLS, "s", "u")     # 4 calls already
    with pytest.raises(budget.BudgetExceeded):
        budget.ask(Boom(), {"deadline": time.time()}, 0, "s", "u")     # no time left


def test_call_timeout_never_exceeds_time_left(monkeypatch):
    seen = {}

    class Spy:
        def complete(self, system, user, json_mode=False, timeout=None):
            seen["t"] = timeout
            return "ok"
    budget.ask(Spy(), {"deadline": time.time() + 0.8}, 0, "s", "u")
    assert 0.3 < seen["t"] <= 0.8                    # min(1.5 s cap, ~0.8 s left)


def test_groq_has_a_wall_clock_limit(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: time.sleep(3))  # network hangs
    t = time.perf_counter()
    with pytest.raises(LLMError):
        GroqLLM().complete("s", "u", timeout=0.5)
    assert time.perf_counter() - t < 1.0


def test_busy_conversation_answers_fast(ctx):
    lock_taken, release = threading.Event(), threading.Event()

    def hold():  # another run of Asha's conversation is in progress
        with service._hold(thread_id("acme", "E101")):
            lock_taken.set()
            release.wait(5)
    threading.Thread(target=hold).start()
    lock_taken.wait(2)
    t = time.perf_counter()
    r = ctx.chat("acme", "E101", "any update?")
    release.set()
    assert r["status"] == "busy" and time.perf_counter() - t < 2.0


@pytest.mark.parametrize("msg", [
    "Move my drop tomorrow to 19:00", "Move my drop tomorrow to 20:30", "yes",
    "Can I leave a bit late tomorrow?", "make it 7 instead", "any update?", "Cancel my pickup tomorrow",
])
def test_every_message_type_within_call_cap(ctx, msg):
    r = ctx.chat("acme", "E101", msg)
    assert r["llm_calls"] <= budget.MAX_LLM_CALLS and r["elapsed_ms"] < LIMIT_S * 1000
