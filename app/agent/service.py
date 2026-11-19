"""Entry points the API calls: chat (employee) and resume (manager).
Data Flow: API -> chat(Identity, text) -> graph.invoke(thread) -> {reply, request_id, status}
           API -> resume(manager, id) -> Command(resume) on the paused thread -> final status.
One conversation = one thread_id = "company:employee". Its state lives in the checkpoint file,
so any server process (e.g. after a restart) can pick it up."""
import threading
import time
from contextlib import contextmanager

from langgraph.types import Command

from app.agent.graph import build_graph, thread_id
from app.agent.templates import render
from app.auth.jwt import Identity
from app.llm.budget import new_deadline
from app.tools import requests as rq

graph = None  # built on startup (tests may replace it)

STATUS = {"applied": "applied", "cancelled": "applied", "pending": "pending_approval",
          "approved": "approved", "approved_cancel": "approved",
          "declined": "declined", "failed": "failed"}

# One run per conversation at a time (employee message vs manager click vs double click)
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
LOCK_WAIT_S = 1.0   # never queue behind another run for long (keeps p95 < 5 s)


class Busy(Exception):
    """Another run of this conversation is still going."""


@contextmanager
def _hold(tid: str):
    """Data Flow: thread_id -> its lock -> wait at most LOCK_WAIT_S -> run | Busy."""
    with _locks_guard:
        lock = _locks.setdefault(tid, threading.Lock())
    if not lock.acquire(timeout=LOCK_WAIT_S):
        raise Busy(tid)
    try:
        yield
    finally:
        lock.release()


def get_graph():
    global graph
    if graph is None:
        graph = build_graph()
    return graph


def chat(who: Identity, message: str) -> dict:
    """Data Flow: text -> lock (<=1 s) -> [paused? -> pending reply | heal]
                  -> graph.invoke(+ deadline = now + 3.5 s for ALL LLM calls) -> reply.
    get_state(cfg) reads the saved checkpoint: .next == ("wait_manager",) means 'paused'."""
    t0 = time.perf_counter()
    tid = thread_id(who.company_id, who.user_id)
    g, cfg = get_graph(), _cfg(tid)
    try:
        with _hold(tid):
            if g.get_state(cfg).next:            # paused for a manager
                req = rq.get_request(who, g.get_state(cfg).values["request_id"])
                if req and req["status"] == "pending_approval":
                    return _pending(g, cfg)      # still waiting: don't start a new run
                # Decided in DB but the thread is still paused (server died mid-resume):
                # resume it so wait_manager reports the recorded outcome (it never re-applies)
                out = g.invoke(Command(resume={"decision": "sync", "by": "system"}), cfg)
            else:
                out = g.invoke({"messages": [{"role": "user", "content": message}],
                                "company_id": who.company_id, "employee_id": who.user_id,
                                "deadline": new_deadline()}, cfg)   # one budget per message
            if g.get_state(cfg).next:            # this message escalated -> now paused
                return _timed(_pending(g, cfg), t0, g.get_state(cfg).values.get("llm_calls", 0))
    except Busy:
        return _timed({"reply": render({"outcome": "busy"}), "request_id": None, "status": "busy"}, t0, 0)
    except Exception:
        return _timed({"reply": render({"outcome": "tool_error"}), "request_id": None,
                       "status": "failed"}, t0, 0)
    outcome = out["result"]["outcome"]
    rid = out.get("request_id") if outcome in STATUS else None
    return _timed({"reply": out["reply"], "request_id": rid, "status": STATUS.get(outcome, outcome)},
                  t0, out.get("llm_calls", 0))


def _timed(resp: dict, t0: float, calls: int) -> dict:
    """Add the two numbers we promise (NFR-1, NFR-2) to every chat response."""
    return {**resp, "llm_calls": calls, "elapsed_ms": round((time.perf_counter() - t0) * 1000)}


def resume(manager: Identity, request_id: int, decision: str, note: str = "") -> dict | None:
    """Manager decision -> resume the paused conversation (works after a restart).
    Data Flow: request (manager's company only) -> its thread_id -> checkpoint still paused
               on THIS request? -> Command(resume=decision) -> wait_manager -> final DB status."""
    req = rq.get_request(manager, request_id)    # filtered by manager's company
    if not req:
        return None
    g, cfg = get_graph(), _cfg(req["thread_id"])
    with _hold(req["thread_id"]):                # 2nd click waits (<=1 s), then sees "not pending"
        req = rq.get_request(manager, request_id)
        snap = g.get_state(cfg)
        if (req["status"] != "pending_approval" or snap.next != ("wait_manager",)
                or snap.values.get("request_id") != request_id):
            return None
        out = g.invoke(Command(resume={"decision": decision, "by": manager.user_id, "note": note}), cfg)
    final = rq.get_request(manager, request_id)
    return {"request_id": request_id, "status": final["status"], "employee_reply": out["reply"]}


def history(who: Identity) -> list[dict]:
    """Data Flow: Identity -> own thread_id -> checkpoint -> messages (incl. manager's outcome)."""
    snap = get_graph().get_state(_cfg(thread_id(who.company_id, who.user_id)))
    return snap.values.get("messages", [])


def _pending(g, cfg) -> dict:
    rid = g.get_state(cfg).values["request_id"]
    return {"reply": render({"outcome": "pending", "request_id": rid}),
            "request_id": rid, "status": "pending_approval"}


def _cfg(tid: str) -> dict:
    return {"configurable": {"thread_id": tid}}
