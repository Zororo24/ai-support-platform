from typing import Any

from app.agents.state import AgentState
from app.rag.answer import generate_answer


def generate(state: AgentState) -> dict[str, Any]:
    answer, citations = generate_answer(state["messages"][-1]["content"], state["retrieved_chunks"])
    return {
        "final_response": answer,
        "citations": citations,
        "outcome": "answered" if citations else "refused",
    }
