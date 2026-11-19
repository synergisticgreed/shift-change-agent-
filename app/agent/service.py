"""Entry points the API calls: chat (employee) and resume (manager).
Data Flow: API -> chat(Identity, text) -> graph.invoke(thread) -> {reply, request_id, status}
           API -> resume(manager, id) -> Command(resume) on the paused thread -> final status.
One conversation = one thread_id = "company:employee". Its state lives in the checkpoint file,
so any server process (e.g. after a restart) can pick it up."""
import threading

from langgraph.types import Command

from app.agent.graph import build_graph, thread_id
from app.agent.templates import render
from app.auth.jwt import Identity
from app.tools import requests as rq

graph = None  # built on startup (tests may replace it)

STATUS = {"applied": "applied", "cancelled": "applied", "pending": "pending_approval",
          "approved": "approved", "approved_cancel": "approved",
          "declined": "declined", "failed": "failed"}

# One run per conversation at a time (employee message vs manager click vs double click)
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock(tid: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(tid, threading.Lock())


def get_graph():
    global graph
    if graph is None:
        graph = build_graph()
    return graph


def chat(who: Identity, message: str) -> dict:
    """Data Flow: text -> [paused? -> pending reply | heal] -> graph.invoke -> reply.
    get_state(cfg) reads the saved checkpoint: .next == ("wait_manager",) means 'paused'."""
    tid = thread_id(who.company_id, who.user_id)
    g, cfg = get_graph(), _cfg(tid)
    try:
        with _lock(tid):
            if g.get_state(cfg).next:            # paused for a manager
                req = rq.get_request(who, g.get_state(cfg).values["request_id"])
                if req and req["status"] == "pending_approval":
                    return _pending(g, cfg)      # still waiting: don't start a new run
                # Decided in DB but the thread is still paused (server died mid-resume):
                # resume it so wait_manager reports the recorded outcome (it never re-applies)
                out = g.invoke(Command(resume={"decision": "sync", "by": "system"}), cfg)
            else:
                out = g.invoke({"messages": [{"role": "user", "content": message}],
                                "company_id": who.company_id, "employee_id": who.user_id}, cfg)
            if g.get_state(cfg).next:            # this message escalated -> now paused
                return _pending(g, cfg)
    except Exception:
        return {"reply": render({"outcome": "tool_error"}), "request_id": None, "status": "failed"}
    outcome = out["result"]["outcome"]
    rid = out.get("request_id") if outcome in STATUS else None
    return {"reply": out["reply"], "request_id": rid, "status": STATUS.get(outcome, outcome)}


def resume(manager: Identity, request_id: int, decision: str, note: str = "") -> dict | None:
    """Manager decision -> resume the paused conversation (works after a restart).
    Data Flow: request (manager's company only) -> its thread_id -> checkpoint still paused
               on THIS request? -> Command(resume=decision) -> wait_manager -> final DB status."""
    req = rq.get_request(manager, request_id)    # filtered by manager's company
    if not req:
        return None
    g, cfg = get_graph(), _cfg(req["thread_id"])
    with _lock(req["thread_id"]):                # 2nd click waits, then sees "not pending"
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
