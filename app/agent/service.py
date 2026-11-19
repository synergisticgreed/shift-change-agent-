"""Entry points the API calls: chat (employee) and resume (manager).
Data Flow: API -> chat(Identity, text) -> graph.invoke(thread) -> {reply, request_id, status}
           API -> resume(manager, id) -> Command(resume) on the paused thread -> final status."""
from langgraph.types import Command

from app.agent.graph import build_graph, thread_id
from app.agent.templates import render
from app.auth.jwt import Identity
from app.tools import requests as rq

graph = None  # built on startup (tests may replace it)

STATUS = {"applied": "applied", "cancelled": "applied", "pending": "pending_approval",
          "approved": "approved", "approved_cancel": "approved", "declined": "declined"}


def get_graph():
    global graph
    if graph is None:
        graph = build_graph()
    return graph


def chat(who: Identity, message: str) -> dict:
    g, cfg = get_graph(), _cfg(thread_id(who.company_id, who.user_id))
    try:
        if g.get_state(cfg).next:       # paused for a manager: don't start a new run
            return _pending(g, cfg)
        out = g.invoke({"messages": [{"role": "user", "content": message}],
                        "company_id": who.company_id, "employee_id": who.user_id}, cfg)
        if g.get_state(cfg).next:       # this message escalated
            return _pending(g, cfg)
    except Exception:
        return {"reply": render({"outcome": "tool_error"}), "request_id": None, "status": "failed"}
    outcome = out["result"]["outcome"]
    rid = out.get("request_id") if outcome in STATUS else None
    return {"reply": out["reply"], "request_id": rid, "status": STATUS.get(outcome, outcome)}


def resume(manager: Identity, request_id: int, decision: str, note: str = "") -> dict | None:
    """Manager decision -> resume the paused conversation (works after restart)."""
    req = rq.get_request(manager, request_id)  # filtered by manager's company
    if not req or req["status"] != "pending_approval":
        return None
    g, cfg = get_graph(), _cfg(req["thread_id"])
    snap = g.get_state(cfg)
    # Only resume if that thread is paused on THIS request
    if snap.next != ("wait_manager",) or snap.values.get("request_id") != request_id:
        return None
    out = g.invoke(Command(resume={"decision": decision, "by": manager.user_id, "note": note}), cfg)
    final = rq.get_request(manager, request_id)
    return {"request_id": request_id, "status": final["status"], "employee_reply": out["reply"]}


def _pending(g, cfg) -> dict:
    rid = g.get_state(cfg).values["request_id"]
    return {"reply": render({"outcome": "pending", "request_id": rid}),
            "request_id": rid, "status": "pending_approval"}


def _cfg(tid: str) -> dict:
    return {"configurable": {"thread_id": tid}}
