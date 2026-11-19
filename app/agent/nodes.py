"""Conversation nodes (design.md §7): understand, clarify, status, offer, reply.
Only `understand` and `reply` use the LLM. Booking nodes (rules + writes) are in booking.py.
Data Flow: user text -> understand(LLM) -> Intent -> [clarify | status | offer | booking.decide]
           -> result -> reply(LLM) -> text. Each node reads state, returns only changed keys."""
import json
import re

from pydantic import ValidationError

from app import clock
from app.agent.intent import Intent, merge
from app.agent.state import identity
from app.agent.templates import render
from app.llm.budget import BudgetExceeded, ask
from app.llm.client import LLM, LLMError
from app.llm.fake import parse as keyword_parse
from app.tools import requests as rq

UNDERSTAND_PROMPT = """You turn an employee's cab message into JSON.
Keys: action (change_time|cancel|status|confirm|decline|unclear), trip_type (pickup|drop|null),
date (YYYY-MM-DD|null), new_time (HH:MM 24h|null).
"previous" is the last intent: use it for follow-ups like "make it 8 instead".
"yes" = confirm, "no" = decline. Never guess a missing value: use null. Reply with JSON only."""

REPLY_PROMPT = """Rewrite the draft as a short, friendly reply to an employee.
Keep every time, date and number exactly. Do not add new offers or facts."""


# ---------- 1. understand (LLM) ----------

def understand(s, llm: LLM):
    """Message -> validated Intent. 1 call + 1 retry; LLM down/slow -> keyword parser.
    Data Flow: last message + previous intent -> budget.ask -> LLM -> Intent -> merge -> state.
    Limits: at most 2 calls here, each within the message's time budget.
    The LLM sees only this employee's words: no rules, no roster, no other company's data."""
    today = clock.now().date().isoformat()
    user = json.dumps({"today": today, "message": s["messages"][-1]["content"],
                       "previous": s.get("intent")})
    calls, intent = 0, None
    for _ in range(2):
        try:
            raw = ask(llm, s, calls, UNDERSTAND_PROMPT, user, json_mode=True)
            calls += 1
            intent = Intent.model_validate_json(raw)
            break
        except ValidationError:
            continue                      # bad JSON -> retry once (if budget allows)
        except LLMError as e:
            calls += 0 if isinstance(e, BudgetExceeded) else 1  # refused = never sent
            intent = Intent(**keyword_parse(s["messages"][-1]["content"],
                                            s.get("intent") or {}, today))
            break                         # LLM down / out of time -> code still handles it
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
    """Data Flow: Identity -> requests.latest_request (own company + own id) -> result."""
    try:
        r = rq.latest_request(identity(s))
    except Exception:
        return {"result": {"outcome": "tool_error"}}
    if not r:
        return {"result": {"outcome": "no_requests"}}
    return {"result": {"outcome": "status", "request_id": r["id"], "status": r["status"]}}


def offer(s):
    """Handle 'yes'/'no' to the last offer. Data Flow: state["offer"] -> state["change"]."""
    o = s.get("offer")
    if s["intent"]["action"] == "decline" or not o:
        outcome = "nothing" if o else "unclear"
        return {"result": {"outcome": outcome}, "offer": None}
    return {"change": o["change"], "offer": None}


def route_offer(s) -> str:
    if s.get("result"):
        return "reply"
    return "escalate" if s["change"].get("escalate") else "apply"


# ---------- 3. reply (LLM, cannot change the outcome) ----------

def reply(s, llm: LLM):
    """Data Flow: result -> template draft -> budget.ask -> LLM rewrite -> fact check -> reply.
    Limits: at most 1 call here, only with time left; otherwise the draft is sent as is.
    The draft is ready BEFORE the call, so a slow LLM can only cost time, never the answer."""
    draft = render(s["result"])
    text, calls = draft, s.get("llm_calls", 0)
    # Questions and errors stay templated (no call); otherwise polish if budget allows
    if s["result"]["outcome"] not in ("clarify", "unclear", "tool_error"):
        try:
            out = ask(llm, s, calls, REPLY_PROMPT, json.dumps({"draft": draft, "result": s["result"]}))
            calls += 1
            text = out if _same_facts(draft, out) else draft
        except LLMError as e:
            calls += 0 if isinstance(e, BudgetExceeded) else 1
    return {"reply": text, "llm_calls": calls,
            "messages": [{"role": "assistant", "content": text}]}


def _same_facts(draft: str, out: str) -> bool:
    """Guard: every date / time / #id in the draft must survive the rewrite."""
    facts = re.findall(r"\d{4}-\d{2}-\d{2}|\d{1,2}:\d{2}|#\d+", draft)
    return bool(out.strip()) and len(out) < 400 and all(f in out for f in facts)
