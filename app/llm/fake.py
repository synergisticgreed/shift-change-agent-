"""Deterministic fake LLM: keyword parser for intents, echo for replies (NFR-5).
Data Flow: understand prompt JSON {message, previous, today} -> parse() -> Intent JSON.
           reply prompt JSON {draft} -> draft returned unchanged."""
import json
import re
from datetime import date, timedelta

TIME_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b")
YES = {"yes", "yeah", "yep", "ok", "okay", "sure", "go ahead", "please do", "do it"}
NO = {"no", "nope", "never mind", "nevermind", "leave it"}


class FakeLLM:
    def __init__(self):
        self.calls = 0  # tests can assert on the budget

    def complete(self, system: str, user: str, json_mode: bool = False) -> str:
        self.calls += 1
        data = json.loads(user)
        if not json_mode:          # reply step: keep the code-written draft
            return data["draft"]
        return json.dumps(parse(data["message"], data.get("previous") or {}, data["today"]))


def parse(msg: str, prev: dict, today: str) -> dict:
    m = msg.lower().strip(" .!?")
    if m in YES:
        return {"action": "confirm"}
    if m in NO:
        return {"action": "decline"}
    if "status" in m or "update" in m:
        return {"action": "status"}

    trip = _trip(m) or prev.get("trip_type")
    out = {"trip_type": _trip(m), "date": _date(m, today), "new_time": _time(m, trip)}
    if "cancel" in m:
        out["action"] = "cancel"
    elif out["new_time"] or any(w in m for w in ("late", "early", "change", "move", "leave", "shift")):
        out["action"] = "change_time"
    else:
        out["action"] = "unclear"
    return out


def _trip(m: str) -> str | None:
    if re.search(r"pick[\s-]?up|morning", m):
        return "pickup"
    if re.search(r"drop|leave|home|tonight|evening", m):
        return "drop"
    return None


def _date(m: str, today: str) -> str | None:
    t = date.fromisoformat(today)
    if "tomorrow" in m:
        return (t + timedelta(days=1)).isoformat()
    if "today" in m or "tonight" in m:
        return today
    return None


def _time(m: str, trip: str | None) -> str | None:
    hit = TIME_RE.search(m)
    if not hit:
        return None
    h, mins, ampm = int(hit[1]), int(hit[2] or 0), hit[3]
    if ampm == "pm" or (ampm is None and trip == "drop" and h < 12):
        h = h % 12 + 12  # "8" for a drop means 20:00
    if ampm == "am" and h == 12:
        h = 0
    return f"{h:02d}:{mins:02d}" if h < 24 and mins < 60 else None
