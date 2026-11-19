"""Intent = the ONLY thing the LLM decides: what the user wants (design.md §6).
Data Flow: LLM JSON -> Intent (Pydantic validation) -> merge(prev) -> state["intent"] -> router."""
import datetime as dt  # alias: a field below is called `date`
from typing import Literal

from pydantic import BaseModel, field_serializer

Action = Literal["change_time", "cancel", "status", "confirm", "decline", "unclear"]


class Intent(BaseModel):
    action: Action
    trip_type: Literal["pickup", "drop"] | None = None
    date: dt.date | None = None
    new_time: dt.time | None = None
    missing: list[str] = []

    @field_serializer("new_time")
    def _hhmm(self, t: dt.time | None):
        return t.strftime("%H:%M") if t else None  # match cabs.depart_at format


# Fields each action needs before code can act
REQUIRED = {"change_time": ["trip_type", "date", "new_time"], "cancel": ["trip_type", "date"]}
FIELDS = ("trip_type", "date", "new_time")


def merge(new: Intent, prev: Intent | None) -> Intent:
    """Fill gaps from the previous turn (FR-9, FR-10).
    Data Flow: prev intent (from checkpoint) + new intent -> one combined intent -> missing list.
      'make it 8 instead'  -> keeps trip + date, new time
      'drop' / 'tomorrow'  -> a bare answer to our question continues the same request"""
    if prev and prev.action in REQUIRED:
        if new.action == "unclear" and any(getattr(new, f) for f in FIELDS):
            new = new.model_copy(update={"action": prev.action})   # bare answer / "8 instead"
        if new.action == prev.action:
            fill = {f: getattr(prev, f) for f in FIELDS if getattr(new, f) is None}
            new = new.model_copy(update=fill)
    # Code (not the LLM) decides what is still missing
    missing = [f for f in REQUIRED.get(new.action, []) if getattr(new, f) is None]
    return new.model_copy(update={"missing": missing})
