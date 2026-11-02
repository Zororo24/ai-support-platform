from typing import Any

from app.agents.state import AgentState

ESCALATE_TEXT = (
    "I'm not sure how to help with that. I can answer questions about our policies, check an "
    "order if you give me the order number (for example ACM-1001), or open a support ticket "
    "so a person can follow up."
)


def escalate(state: AgentState) -> dict[str, Any]:
    return {"final_response": ESCALATE_TEXT, "outcome": "other"}
