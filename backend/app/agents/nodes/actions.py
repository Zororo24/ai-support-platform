"""Tool-calling nodes. They act strictly for the principal in the graph config."""

from typing import Any
from uuid import UUID

from langchain_core.runnables import RunnableConfig

from app.agents.state import AgentState
from app.agents.tools.create_ticket import create_ticket as create_ticket_tool
from app.agents.tools.lookup_order import lookup_order as lookup_order_tool
from app.core.llm import LLMUnavailable, chat_json


def _principal(state: AgentState, config: RunnableConfig):
    db = config["configurable"]["db"]
    user = config["configurable"]["user"]
    # Defence in depth: the state's identity must match the authenticated user.
    if str(user.id) != state["user_id"] or str(user.tenant_id) != state["tenant_id"]:
        raise PermissionError("graph state does not match authenticated principal")
    return db, user


def lookup_order(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    db, _ = _principal(state, config)
    number = state.get("order_number")
    if not number:
        return {
            "final_response": "Which order would you like me to check? Please share the order "
            "number (for example ACM-1001).",
            "tool_result": None,
            "outcome": "order_needs_number",
        }
    order = lookup_order_tool(db, tenant_id=UUID(state["tenant_id"]), order_number=number)
    if order is None:
        return {
            "final_response": f"I couldn't find an order {number} on your account. "
            "Please double-check the number.",
            "tool_result": None,
            "outcome": "order_not_found",
        }
    return {
        "final_response": f"Order {order.order_number} is currently {order.status}.",
        "tool_result": {"order": order},
        "outcome": "order_found",
    }


TICKET_PROMPT = (
    "Write a support ticket for a human agent from this conversation. Capture the customer's "
    "actual problem and any order numbers or specifics they mentioned. Do not invent facts.\n"
    'Reply as JSON: {"subject": "<max 80 chars>", "body": "<2-5 sentence summary of the issue>"}'
)


def _draft_ticket(messages: list[dict[str, str]]) -> tuple[str, str]:
    transcript = "\n".join(f"{m['role']}: {m['content']}" for m in messages)
    data = chat_json(
        [
            {"role": "system", "content": TICKET_PROMPT},
            {"role": "user", "content": f"Conversation:\n{transcript}"},
        ]
    )
    subject, body = data.get("subject"), data.get("body")
    if not (isinstance(subject, str) and subject.strip() and isinstance(body, str) and body.strip()):
        raise LLMUnavailable("LLM returned an unusable ticket draft")
    return " ".join(subject.split())[:80], body.strip()


def create_ticket(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    db, user = _principal(state, config)
    subject, body = _draft_ticket(state["messages"])
    ticket = create_ticket_tool(
        db,
        user=user,
        subject=subject,
        body=body,
        conversation_id=config["configurable"].get("conversation_id"),
    )
    return {
        "final_response": f"I've opened ticket #{str(ticket.id)[:8]} so a teammate can follow up. "
        "You can track it on the Tickets page.",
        "tool_result": {"ticket": ticket},
        "outcome": "ticket_created",
    }
