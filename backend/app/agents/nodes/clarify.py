from typing import Any

from app.agents.state import AgentState

CLARIFY_TEXT = (
    "I want to make sure I help with the right thing. Are you asking about a policy "
    "(like returns or shipping), the status of an order, or would you like me to open a "
    "support ticket?"
)


def clarify(state: AgentState) -> dict[str, Any]:
    return {"final_response": CLARIFY_TEXT, "outcome": "clarify"}
