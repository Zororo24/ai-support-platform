from functools import lru_cache

from langgraph.graph import END, START, StateGraph

from app.agents.nodes.actions import create_ticket, lookup_order
from app.agents.nodes.clarify import clarify
from app.agents.nodes.classify import classify, route_after_classify
from app.agents.nodes.escalate import escalate
from app.agents.nodes.generate import generate
from app.agents.nodes.retrieve import retrieve
from app.agents.state import AgentState


def build_graph():
    g = StateGraph(AgentState)
    g.add_node("classify", classify)
    g.add_node("retrieve", retrieve)
    g.add_node("generate", generate)
    g.add_node("lookup_order", lookup_order)
    g.add_node("create_ticket", create_ticket)
    g.add_node("escalate", escalate)
    g.add_node("clarify", clarify)

    g.add_edge(START, "classify")
    g.add_conditional_edges(
        "classify",
        route_after_classify,
        {
            "retrieve": "retrieve",
            "lookup_order": "lookup_order",
            "create_ticket": "create_ticket",
            "escalate": "escalate",
            "clarify": "clarify",
        },
    )
    g.add_edge("retrieve", "generate")
    for terminal in ("generate", "lookup_order", "create_ticket", "escalate", "clarify"):
        g.add_edge(terminal, END)
    return g.compile()


@lru_cache
def get_graph():
    return build_graph()
