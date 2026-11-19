"""Per-message LLM limits: max 4 calls (NFR-2) and one shared time budget (NFR-1, p95 < 5 s).
Data Flow: service stamps state["deadline"] = now + LLM_BUDGET_S when a message arrives
           -> every LLM call goes through ask() -> ask() checks calls + time left
           -> call gets timeout = min(per-call cap, time left) -> over budget: LLMError
           -> the node falls back to code (keyword parser / template), never waits longer.

Time plan for one message (worst case):
    our code (auth + DB + rules + checkpoints)   ~0.05 s  (measured p95 39 ms)
    LLM budget, all calls together                3.50 s  (each call capped at 1.5 s)
    lock waits (conversation lock / SQLite lock)  capped separately (see service, store)
    -> stays under 5 s even when the LLM is slow or hanging."""
import time

from app.config import settings
from app.llm.client import LLM, LLMError

MAX_LLM_CALLS = 4     # hard cap per message; the fixed graph needs at most 3
MIN_CALL_S = 0.3      # not worth starting a call with less time than this


class BudgetExceeded(LLMError):
    """Call refused BEFORE reaching the LLM (so it does not count as a call)."""


def new_deadline() -> float:
    """Wall-clock deadline (epoch seconds) so it survives the checkpoint (plain float)."""
    return time.time() + settings.llm_budget_s


def ask(llm: LLM, s: dict, calls: int, system: str, user: str, json_mode: bool = False) -> str:
    """The ONLY way nodes call the LLM. `calls` = calls already made for this message.
    Raises LLMError (caller falls back) when the call cap or the time budget is used up."""
    left = s.get("deadline", 0) - time.time()
    if calls >= MAX_LLM_CALLS:
        raise BudgetExceeded("call budget used")
    if left < MIN_CALL_S:
        raise BudgetExceeded("time budget used")
    return llm.complete(system, user, json_mode=json_mode,
                        timeout=min(settings.llm_timeout_s, left))
