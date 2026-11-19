"""Graph nodes (design.md §7). Only `understand` and `reply` use the LLM;
everything that changes data is plain code.
Data Flow: user text -> understand(LLM) -> Intent -> decide(tools+rules) -> apply/escalate(DB)
           -> result -> reply(LLM) -> text. Each node reads state, returns only changed keys."""
import json
import re

from langgraph.types import interrupt
from pydantic import ValidationError

from app import clock
from app.agent.intent import Intent, merge
from app.agent.templates import render
from app.auth.jwt import Identity
from app.llm.client import LLM, LLMError
from app.llm.fake import parse as keyword_parse
from app.rules.engine import check_cutoff, nearest_free, trip_at
from app.tools import requests as rq
from app.tools import roster

MAX_LLM_CALLS = 4

UNDERSTAND_PROMPT = """You turn an employee's cab message into JSON.
Keys: action (change_time|cancel|status|confirm|decline|unclear), trip_type (pickup|drop|null),
date (YYYY-MM-DD|null), new_time (HH:MM 24h|null).
"previous" is the last intent: use it for follow-ups like "make it 8 instead".
"yes" = confirm, "no" = decline. Never guess a missing value: use null. Reply with JSON only."""

REPLY_PROMPT = """Rewrite the draft as a short, friendly reply to an employee.
Keep every time, date and number exactly. Do not add new offers or facts."""


def _who(s) -> Identity:
    return Identity(s["company_id"], s["employee_id"], "employee")


# ---------- 1. understand (LLM) ----------

def understand(s, llm: LLM):
    """Message -> validated Intent. 1 call + 1 retry; LLM down -> keyword parser.
    Data Flow: last message + previous intent -> LLM -> Intent -> merge -> state["intent"]."""
    today = clock.now().date().isoformat()
    user = json.dumps({"today": today, "message": s["messages"][-1]["content"],
                       "previous": s.get("intent")})
    calls, intent = 0, None
    for _ in range(2):
        calls += 1
        try:
            intent = Intent.model_validate_json(llm.complete(UNDERSTAND_PROMPT, user, json_mode=True))
            break
        except ValidationError:
            continue                      # bad JSON -> retry once
        except LLMError:
            intent = Intent(**keyword_parse(s["messages"][-1]["content"],
                                            s.get("intent") or {}, today))
            break                         # LLM down -> code still handles it
    intent = merge(intent or Intent(action="unclear"), _prev(s))
    # An offer only survives into the very next "yes"/"no" (no stale offers)
    keep_offer = intent.action in ("confirm", "decline")
    return {"intent": intent.model_dump(mode="json"), "llm_calls": calls,
            "result": None, "change": None, "offer": s.get("offer") if keep_offer else None}


def _prev(s) -> Intent | None:
    return Intent(**s["intent"]) if s.get("intent") else None


def route_intent(s) -> str:
    i = s["intent"]
    if i["action"] == "status":
        return "status"
    if i["action"] in ("confirm", "decline"):
        return "offer"
    if i["action"] == "unclear" or i["missing"]:
        return "clarify"
    return "decide"


# ---------- 2. simple branches (no LLM) ----------

def clarify(s):
    """FR-9: ask, don't guess."""
    i = s["intent"]
    if i["action"] == "unclear":
        return {"result": {"outcome": "unclear"}}
    return {"result": {"outcome": "clarify", "missing": i["missing"]}}


def status(s):
    try:
        r = rq.latest_request(_who(s))
    except Exception:
        return {"result": {"outcome": "tool_error"}}
    if not r:
        return {"result": {"outcome": "no_requests"}}
    return {"result": {"outcome": "status", "request_id": r["id"], "status": r["status"]}}


def offer(s):
    """Handle 'yes'/'no' to the last offer."""
    o = s.get("offer")
    if s["intent"]["action"] == "decline" or not o:
        outcome = "nothing" if o else "unclear"
        return {"result": {"outcome": outcome}, "offer": None}
    return {"change": o["change"], "offer": None}


def route_offer(s) -> str:
    if s.get("result"):
        return "reply"
    return "escalate" if s["change"].get("escalate") else "apply"


# ---------- 3. decide (code: roster + seats + cut-off) ----------

def decide(s):
    """Data Flow: Intent -> roster tools (trip, cab options, cut-off) -> rules
    -> either state["change"] (go apply) or state["result"] + offer (go reply)."""
    i, who = s["intent"], _who(s)
    day, trip = i["date"], i["trip_type"]
    base = {"trip": trip, "date": day}
    try:
        cur = roster.get_trip(who, day, trip)
        if not cur:
            return {"result": {"outcome": "no_booking", **base}, "offer": None}
        options = roster.list_options(who, day, trip) if i["action"] == "change_time" else []
        cutoff = roster.get_cutoff(who, trip)
    except Exception:
        return {"result": {"outcome": "tool_error"}, "offer": None}

    ch = {"date": day, "trip_type": trip, "from_cab": cur["cab_id"], "to_cab": None,
          "thread_id": f"{who.company_id}:{who.user_id}"}
    times = [cur["depart_at"]]

    if i["action"] == "change_time":
        target = next((o for o in options if o["depart_at"] == i["new_time"]), None)
        if not target:
            free = ", ".join(o["depart_at"] for o in options if o["booked"] < o["capacity"])
            return {"result": {"outcome": "no_cab", "time": i["new_time"], "options": free, **base},
                    "offer": None}
        if target["id"] == cur["cab_id"]:  # already on that cab -> nothing to do
            return {"result": {"outcome": "same_time", "time": target["depart_at"], **base},
                    "offer": None}
        ch["to_cab"] = target["id"]
        times.append(target["depart_at"])

    # Cut-off: earliest affected trip must be far enough away
    first = min(trip_at(day, t) for t in times)
    if not check_cutoff(cutoff, first, clock.now()):
        ch.update(escalate=True, reason="after cut-off")
        return {"result": {"outcome": "cutoff", "cutoff_h": cutoff // 60,
                           "trip_time": first.strftime("%H:%M"), **base},
                "offer": {"kind": "escalate", "change": ch}}

    # Seats: offer the nearest free cab instead
    if ch["to_cab"] and target["booked"] >= target["capacity"]:
        return _offer_alternative(options, ch, i["new_time"], base, "cab_full")
    return {"change": ch, "offer": None}


def _offer_alternative(options, ch, want, base, outcome):
    # Never offer the full cab itself or the cab the employee is already on
    alt = nearest_free([o for o in options if o["id"] not in (ch["to_cab"], ch["from_cab"])], want)
    if not alt:
        return {"result": {"outcome": "no_seats", **base}, "offer": None}
    alt_ch = {**ch, "to_cab": alt["id"]}
    return {"result": {"outcome": outcome, "time": want, "alt_time": alt["depart_at"], **base},
            "offer": {"kind": "alternative", "change": alt_ch}}


def route_decide(s) -> str:
    return "reply" if s.get("result") else "apply"


# ---------- 4. apply (code, one transaction) ----------

def apply(s):
    """Data Flow: state["change"] -> requests.apply_change (one txn) -> result (applied | why not)."""
    ch, who = s["change"], _who(s)
    base = {"trip": ch["trip_type"], "date": ch["date"]}
    try:
        rid = rq.apply_change(who, ch, actor=who.user_id)
    except rq.RuleBroken as e:
        return _apply_failed(s, str(e), ch, base)
    except Exception:
        return {"result": {"outcome": "tool_error"}}
    if not ch["to_cab"]:
        return {"result": {"outcome": "cancelled", **base}, "request_id": rid}
    t = _cab_time(who, ch)
    return {"result": {"outcome": "applied", "time": t, "cab": ch["to_cab"], **base},
            "request_id": rid, "intent": None}  # done -> fresh context


def _apply_failed(s, why, ch, base):
    """Rule failed at save time (race or stale offer)."""
    if why == "duplicate":
        return {"result": {"outcome": "duplicate"}}
    if why == "cutoff":
        return {"result": {"outcome": "cutoff", "cutoff_h": roster.get_cutoff(_who(s), ch["trip_type"]) // 60,
                           "trip_time": _cab_time(_who(s), ch, "from_cab"), **base},
                "offer": {"kind": "escalate", "change": {**ch, "escalate": True, "reason": "after cut-off"}}}
    options = roster.list_options(_who(s), ch["date"], ch["trip_type"])
    return _offer_alternative(options, ch, _cab_time(_who(s), ch), base, "seat_taken")


def _cab_time(who, ch, key: str = "to_cab") -> str:
    """Departure time of the change's new (or old) cab."""
    opts = roster.list_options(who, ch["date"], ch["trip_type"])
    return next(o["depart_at"] for o in opts if o["id"] == ch[key])


# ---------- 5. escalate + wait for manager (FR-7, FR-8) ----------

def escalate(s):
    try:
        rid = rq.create_pending(_who(s), s["change"])
    except Exception:
        return {"result": {"outcome": "tool_error"}}
    return {"request_id": rid, "result": {"outcome": "pending", "request_id": rid}}


def route_escalate(s) -> str:
    return "reply" if s["result"]["outcome"] == "tool_error" else "wait_manager"


def wait_manager(s):
    """Pause here. State is saved; the run resumes when a manager decides.
    Data Flow: interrupt() -> checkpoint saved -> (manager API) Command(resume=decision)
               -> apply_change(override cut-off) or close_request -> result."""
    d = interrupt({"request_id": s["request_id"]})  # -> {"decision", "by", "note"}
    who, ch, rid = _who(s), s["change"], s["request_id"]
    base = {"trip": ch["trip_type"], "date": ch["date"], "request_id": rid}
    if d["decision"] != "approve":
        rq.close_request(who.company_id, rid, "declined", d["by"], d.get("note", ""))
        return {"result": {"outcome": "declined", **base}, "llm_calls": 0}
    try:
        rq.apply_change(who, ch, actor=d["by"], status="approved",
                        override_cutoff=True, request_id=rid)
    except rq.RuleBroken:  # cab filled while waiting
        rq.close_request(who.company_id, rid, "failed", d["by"], "cab full")
        return {"result": {"outcome": "no_seats", **base}, "llm_calls": 0}
    if not ch["to_cab"]:  # approved cancel has no new cab time
        return {"result": {"outcome": "approved_cancel", **base}, "llm_calls": 0, "intent": None}
    return {"result": {"outcome": "approved", "time": _cab_time(who, ch), **base},
            "llm_calls": 0, "intent": None}


# ---------- 6. reply (LLM, cannot change the outcome) ----------

def reply(s, llm: LLM):
    """Data Flow: result -> template draft -> LLM rewrite -> fact check -> state["reply"] + messages."""
    draft = render(s["result"])
    text, calls = draft, s.get("llm_calls", 0)
    # Questions and errors stay templated; otherwise polish if budget allows
    if s["result"]["outcome"] not in ("clarify", "unclear", "tool_error") and calls < MAX_LLM_CALLS:
        calls += 1
        try:
            out = llm.complete(REPLY_PROMPT, json.dumps({"draft": draft, "result": s["result"]}))
            text = out if _same_facts(draft, out) else draft
        except LLMError:
            pass
    return {"reply": text, "llm_calls": calls,
            "messages": [{"role": "assistant", "content": text}]}


def _same_facts(draft: str, out: str) -> bool:
    """Guard: every date / time / #id in the draft must survive the rewrite."""
    facts = re.findall(r"\d{4}-\d{2}-\d{2}|\d{1,2}:\d{2}|#\d+", draft)
    return bool(out.strip()) and len(out) < 400 and all(f in out for f in facts)
