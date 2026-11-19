"""What the agent remembers per conversation. Saved by the checkpointer after every step.
Data Flow: service input -> node returns partial dict -> merged into state -> checkpoint file.
While paused for a manager, `change` + `request_id` are what resume needs. They are on disk,
so the decision can arrive hours later, on a different server process."""
from operator import add
from typing import Annotated, TypedDict

from app.auth.jwt import Identity


class AgentState(TypedDict, total=False):
    company_id: str             # from JWT, never from the LLM
    employee_id: str            # from JWT
    messages: Annotated[list, add]  # chat history (new items are appended)
    intent: dict | None         # last Intent (JSON) -> context for "make it 8 instead"
    change: dict | None         # the concrete change code decided to make
    offer: dict | None          # what "yes" means next turn: alternative | escalate
    result: dict | None         # outcome decided by code
    request_id: int | None
    llm_calls: int              # budget counter for this message
    reply: str


def identity(s: AgentState) -> Identity:
    """Data Flow: state (ids copied from the JWT at /chat) -> Identity for tools."""
    return Identity(s["company_id"], s["employee_id"], "employee")
