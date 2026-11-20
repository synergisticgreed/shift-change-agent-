"""Evaluation runner: edge cases from the case study -> eval/cases.jsonl + EVALUATION.md.
Usage: python -m eval.run            (fake LLM; set LLM_PROVIDER=groq to use the real one)
Data Flow: for each case -> fresh Env (own DB, checkpoint, clock) -> HTTP calls -> actual
           -> pass = behaviour matches AND every message <= 4 LLM calls AND < 5 s
           -> one JSON line {id, input, expected, actual, pass, notes} per case."""
import json
import logging
import threading
from pathlib import Path

from sqlalchemy import select

from app.agent import service
from app.agent.graph import build_graph
from app.db import store
from app.db.schema import shift_requests
from app.llm.client import LLMError, get_llm
from app.tools import roster
from eval.harness import TRIP_DAY, Env

OUT, REPORT = Path(__file__).with_name("cases.jsonl"), Path(__file__).parents[1] / "EVALUATION.md"
CASES = []


def case(cid, given, expected, notes):
    """Register a case. The function gets an Env and returns (actual, ok)."""
    def wrap(fn):
        CASES.append({"id": cid, "input": given, "expected": expected, "notes": notes, "fn": fn})
        return fn
    return wrap


def say(env, *msgs, who=("acme", "E101")):
    """Send messages in order; return the last response."""
    for m in msgs:
        r = env.chat(*who, m)
    return r


# ---------- core flow ----------

@case("E01", "Move my drop tomorrow to 19:00", "applied, now on A-D2", "Happy path")
def _(env):
    r = say(env, "Move my drop tomorrow to 19:00")
    return f"{r['status']}, now on {env.cab('acme', 'E101')}", r["status"] == "applied" and env.cab("acme", "E101") == "A-D2"


@case("E02", "Cancel my pickup tomorrow", "applied, pickup removed", "Cancel")
def _(env):
    r = say(env, "Cancel my pickup tomorrow")
    return f"{r['status']}, pickup={env.cab('acme', 'E101', 'pickup')}", r["status"] == "applied" and not env.cab("acme", "E101", "pickup")


@case("E03", "any update?", "status / no_requests", "Status check calls the read tool only")
def _(env):
    r = say(env, "any update?")
    return r["status"], r["status"] == "no_requests"


# ---------- rules ----------

@case("E04", "17:00, 1h before 18:00 drop: Move my drop today to 19:00",
      "cutoff + reason (2h) + offer manager; trip unchanged", "Cut-off rejected with reason + next option")
def _(env):
    env.late()
    r = say(env, "Move my drop today to 19:00")
    ok = r["status"] == "cutoff" and "2h" in r["reply"] and "manager" in r["reply"] and env.cab("acme", "E101") == "A-D1"
    return f"{r['status']}: {r['reply']}", ok


@case("E05", "Move my drop tomorrow to 20:30 (full) -> yes", "cab_full offers 20:00 -> applied A-D3",
      "Full cab gets nearest free alternative")
def _(env):
    first = say(env, "Move my drop tomorrow to 20:30")
    r = say(env, "yes")
    ok = first["status"] == "cab_full" and "20:00" in first["reply"] and env.cab("acme", "E101") == "A-D3"
    return f"{first['status']} -> {r['status']}, now on {env.cab('acme', 'E101')}", ok


@case("E06", "18:30, drop at 18:00 already left: Move my drop today to 20:00 -> yes",
      "trip_passed, no escalation offer", "Departed trips are never changeable")
def _(env):
    env.at(TRIP_DAY, 18, 30)
    r = say(env, "Move my drop today to 20:00")
    nxt = say(env, "yes")
    return f"{r['status']} -> {nxt['status']}", r["status"] == "trip_passed" and nxt["status"] == "unclear"


@case("E07", "Globex (4h rule) at 13:30, drop 17:00: Move my drop today to 19:00",
      "cutoff with 4h", "Each company uses its own cut-off")
def _(env):
    env.at(TRIP_DAY, 13, 30)
    r = say(env, "Move my drop today to 19:00", who=("globex", "G201"))
    return f"{r['status']}: {r['reply']}", r["status"] == "cutoff" and "4h" in r["reply"]


# ---------- ambiguity ----------

@case("E08", "Can I leave a bit late tomorrow?", "clarify question listing free cabs, nothing changed",
      "Ambiguous request -> question, not a guess")
def _(env):
    r = say(env, "Can I leave a bit late tomorrow?")
    ok = r["status"] == "clarify" and r["reply"].endswith("?") and "19:00" in r["reply"] and env.cab("acme", "E101") == "A-D1"
    return r["reply"], ok


@case("E09", "a bit late tomorrow -> make it 7 instead", "applied A-D2 (19:00)", "Context kept across messages")
def _(env):
    r = say(env, "Can I leave a bit late tomorrow?", "make it 7 instead")
    return f"{r['status']}, now on {env.cab('acme', 'E101')}", r["status"] == "applied" and env.cab("acme", "E101") == "A-D2"


@case("E10", "change my cab time -> drop -> tomorrow -> 7 pm", "3 questions then applied A-D2",
      "Step-by-step clarification")
def _(env):
    r = say(env, "I want to change my cab time", "drop", "tomorrow", "7 pm")
    return f"{r['status']}, now on {env.cab('acme', 'E101')}", r["status"] == "applied" and env.cab("acme", "E101") == "A-D2"


@case("E11", "move my pickup and drop tomorrow to 9", "clarify: pickup or drop?", "Two trips in one message")
def _(env):
    r = say(env, "move my pickup and drop tomorrow to 9")
    return r["reply"], r["status"] == "clarify" and "pickup or your drop" in r["reply"]


# ---------- escalation ----------

def _escalate(env):
    env.late()
    say(env, "Move my drop today to 19:00")
    return say(env, "yes")["request_id"]


@case("E12", "late change -> yes -> manager approves", "approved, A-D2, employee told",
      "Escalation + manager approval")
def _(env):
    rid = _escalate(env)
    out = env.decide(rid).json()
    hist = env.get("/chat/history", "acme", "E101").json()
    ok = out["status"] == "approved" and env.cab("acme", "E101") == "A-D2" and "approved" in hist[-1]["content"]
    return f"{out['status']}, now on {env.cab('acme', 'E101')}", ok


@case("E13", "late change -> yes -> manager rejects", "declined, trip unchanged", "Manager rejection")
def _(env):
    out = env.decide(_escalate(env), "reject").json()
    return f"{out['status']}, still on {env.cab('acme', 'E101')}", out["status"] == "declined" and env.cab("acme", "E101") == "A-D1"


@case("E14", "late change -> yes -> RESTART -> manager approves", "approved after restart",
      "Paused state is read back from the checkpoint file (full multi-process test: tests/test_restart.py)")
def _(env):
    rid = _escalate(env)
    env.restart()
    out = env.decide(rid).json()
    return out["status"], out["status"] == "approved" and env.cab("acme", "E101") == "A-D2"


@case("E15", "manager clicks approve twice at once", "one 200, other 404/409, approved once",
      "Decision applies exactly once")
def _(env):
    rid, codes = _escalate(env), []
    _together(lambda: codes.append(env.decide(rid).status_code), lambda: codes.append(env.decide(rid).status_code))
    return str(sorted(codes)), codes.count(200) == 1 and env.cab("acme", "E101") == "A-D2"


# ---------- isolation ----------

@case("E16", "Globex employee asks for 20:00 (an Acme-only time)", "no_cab listing only Globex times",
      "Tenant isolation in the agent")
def _(env):
    r = say(env, "Move my drop tomorrow to 20:00", who=("globex", "G201"))
    return r["reply"], r["status"] == "no_cab" and "17:00, 19:00" in r["reply"]


@case("E17", "Globex token reads an Acme request + Globex manager decides it", "404 / empty / 404",
      "Tenant isolation in the API")
def _(env):
    rid = _escalate(env)
    a = env.get(f"/requests/{rid}", "globex", "G201").status_code
    b = env.get("/manager/pending", "globex", "M2", "manager").json()
    c = env.decide(rid, company="globex", manager="M2").status_code
    return f"{a} / {b} / {c}", (a, b, c) == (404, [], 404)


@case("E18", "Bearer not-a-token", "401 Please log in again.", "Bad token")
def _(env):
    res = env.client.post("/chat", json={"message": "hi"}, headers={"Authorization": "Bearer x"})
    return f"{res.status_code} {res.json()['detail']}", res.status_code == 401


# ---------- failures ----------

@case("E19", "roster tool times out", "plain 'Nothing was changed' message, trip unchanged", "Tool failure")
def _(env):
    real = roster.get_trip
    roster.get_trip = lambda *a, **k: (_ for _ in ()).throw(TimeoutError("db slow"))
    try:
        r = say(env, "Move my drop tomorrow to 19:00")
    finally:
        roster.get_trip = real
    ok = "Nothing was changed" in r["reply"] and "Traceback" not in r["reply"] and env.cab("acme", "E101") == "A-D1"
    return r["reply"], ok


@case("E20", "LLM down for every call", "applied by code (keyword parser + template)", "LLM outage")
def _(env):
    class Down:
        def complete(self, *a, **k):
            raise LLMError("down")
    env.llm = Down()
    service.graph = build_graph(env.llm, env.checkpoint)
    r = say(env, "Move my drop tomorrow to 19:00")
    return r["status"], r["status"] == "applied" and env.cab("acme", "E101") == "A-D2"


# ---------- concurrency (bonus) ----------

@case("E21", "Asha + Eli ask for the last 20:00 seat at the same time", "exactly one gets A-D3",
      "Last seat (bonus)")
def _(env):
    out = {}
    _together(lambda: out.update(asha=say(env, "Move my drop tomorrow to 20:00")["status"]),
              lambda: out.update(eli=say(env, "Move my drop tomorrow to 20:00", who=("acme", "E105"))["status"]))
    winners = [e for e in ("E101", "E105") if env.cab("acme", e) == "A-D3"]
    return f"{out}, on A-D3: {winners}", len(winners) == 1


@case("E22", "same message sent twice at once", "one request created", "Double submit (bonus)")
def _(env):
    _together(lambda: say(env, "Move my drop tomorrow to 19:00"), lambda: say(env, "Move my drop tomorrow to 19:00"))
    n = len(_requests())
    return f"{n} request(s), now on {env.cab('acme', 'E101')}", n == 1 and env.cab("acme", "E101") == "A-D2"


# ---------- helpers ----------

def _together(*fns):
    """Start all functions at the same instant (barrier) and wait for them."""
    gate = threading.Barrier(len(fns))
    threads = [threading.Thread(target=lambda f=f: (gate.wait(), f())) for f in fns]
    [t.start() for t in threads]
    [t.join() for t in threads]


def _requests():
    with store.engine.connect() as conn:
        return conn.execute(select(shift_requests.c.id)).all()


# ---------- runner ----------

def run() -> list[dict]:
    rows = []
    for c in CASES:
        env = Env(get_llm())
        try:
            actual, ok = c["fn"](env)
        except Exception as e:                       # a crash is a failed case, not a crashed run
            actual, ok = f"error: {type(e).__name__}: {e}", False
        budget_ok = env.max_calls <= 4 and env.max_ms < 5000
        notes = f"{c['notes']}. max {env.max_calls} LLM calls, max {env.max_ms} ms per message"
        rows.append({"id": c["id"], "input": c["input"], "expected": c["expected"],
                     "actual": actual, "pass": bool(ok and budget_ok), "notes": notes})
    OUT.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    _report(rows)
    return rows


def _report(rows):
    passed = sum(r["pass"] for r in rows)
    lines = ["# Evaluation", "", f"**{passed}/{len(rows)} passed.** Generated by `python -m eval.run`; "
             "raw results in `eval/cases.jsonl`.", "", "| id | pass | input | expected | notes |", "|---|---|---|---|---|"]
    lines += [f"| {r['id']} | {'✅' if r['pass'] else '❌'} | {r['input']} | {r['expected']} | {r['notes']} |" for r in rows]
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    # Failure cases (E19, E20) log full traces on purpose; keep the console readable
    logging.getLogger("shift_agent").setLevel(logging.CRITICAL)
    results = run()
    for r in results:
        print(f"{r['id']}  {'PASS' if r['pass'] else 'FAIL'}  {r['actual'][:90]}")
    print(f"\n{sum(r['pass'] for r in results)}/{len(results)} passed -> {OUT.name}, {REPORT.name}")
