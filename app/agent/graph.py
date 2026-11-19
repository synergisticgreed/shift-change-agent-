"""Wire the nodes into a fixed graph (architecture.md §3).

START -> understand -> clarify | status | offer | decide
decide -> apply | reply          offer -> apply | escalate | reply
apply -> reply                   escalate -> wait_manager (pause) -> reply -> END

Data Flow: service.invoke(state, thread_id) -> nodes in order above -> final state
           (every step saved to the SQLite checkpoint file)."""
import sqlite3
from functools import partial

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from app.agent import booking as b
from app.agent import nodes as n
from app.agent.state import AgentState
from app.config import settings
from app.llm.client import LLM, get_llm


def build_graph(llm: LLM | None = None, checkpoint_path: str | None = None):
    llm = llm or get_llm()
    g = StateGraph(AgentState)

    g.add_node("understand", partial(n.understand, llm=llm))
    g.add_node("clarify", n.clarify)
    g.add_node("status", n.status)
    g.add_node("offer", n.offer)
    g.add_node("decide", b.decide)          # booking nodes: rules + writes
    g.add_node("apply", b.apply)
    g.add_node("escalate", b.escalate)
    g.add_node("wait_manager", b.wait_manager)
    g.add_node("reply", partial(n.reply, llm=llm))

    g.add_edge(START, "understand")
    g.add_conditional_edges("understand", n.route_intent, ["clarify", "status", "offer", "decide"])
    g.add_edge("clarify", "reply")
    g.add_edge("status", "reply")
    g.add_conditional_edges("offer", n.route_offer, ["apply", "escalate", "reply"])
    g.add_conditional_edges("decide", b.route_decide, ["apply", "reply"])
    g.add_edge("apply", "reply")
    g.add_conditional_edges("escalate", b.route_escalate, ["wait_manager", "reply"])
    g.add_edge("wait_manager", "reply")
    g.add_edge("reply", END)

    # PERSIST (FR-8): after EVERY node, LangGraph writes the full state to this SQLite file,
    # keyed by thread_id. A run stopped by interrupt() is just a saved state whose
    # "next" node is wait_manager. A new process opens the same file and can resume it.
    # Data Flow: node returns -> state merged -> SqliteSaver.put(thread_id, state) -> disk
    conn = sqlite3.connect(checkpoint_path or settings.checkpoint_db, check_same_thread=False)
    return g.compile(checkpointer=SqliteSaver(conn))


def thread_id(company_id: str, employee_id: str) -> str:
    """One conversation per employee, namespaced by company."""
    return f"{company_id}:{employee_id}"
