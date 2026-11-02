import logging
import re
from typing import Any

from app.agents.state import AgentState
from app.core.llm import chat_json

logger = logging.getLogger(__name__)

INTENTS = {"knowledge", "order", "ticket", "other"}
CONFIDENCE_THRESHOLD = 0.6
HISTORY_TURNS = 4
ORDER_RE = re.compile(r"\b([A-Za-z]{3}-\d{3,})\b")

SYSTEM_PROMPT = (
    "You route messages for a customer support assistant. Classify the customer's LATEST "
    "message (use earlier turns only to resolve references like 'yes' or 'that order') into "
    "exactly one intent:\n"
    '- "knowledge": a question answerable from FAQs/policies (returns, shipping, warranty...)\n'
    '- "order": asks about the status/location of an order\n'
    '- "ticket": wants a human, has a complaint, needs an exception, or asks to open a ticket\n'
    '- "other": greetings, thanks, or anything unrelated to support\n'
    "Give a confidence between 0 and 1; use a low value when the message is ambiguous.\n"
    'Reply as JSON: {"intent": "<intent>", "confidence": <number>}'
)


def extract_order_number(text: str) -> str | None:
    match = ORDER_RE.search(text)
    return match.group(1).upper() if match else None


def _transcript(messages: list[dict[str, str]]) -> str:
    return "\n".join(f"{m['role']}: {m['content']}" for m in messages[-(HISTORY_TURNS + 1) : -1])


def classify(state: AgentState) -> dict[str, Any]:
    messages = state["messages"]
    latest = messages[-1]["content"]
    history = _transcript(messages)
    data = chat_json(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (f"Earlier turns:\n{history}\n\n" if history else "")
                + f"Latest message: {latest}",
            },
        ]
    )

    intent = data.get("intent")
    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    if intent not in INTENTS:
        intent, confidence = "other", 0.0  # unusable verdict: ask the customer to clarify

    # The order number is parsed, not guessed: latest message first, then recent customer turns.
    order_number = extract_order_number(latest)
    if order_number is None:
        for m in reversed(messages[:-1][-HISTORY_TURNS:]):
            if m["role"] == "user" and (order_number := extract_order_number(m["content"])):
                break

    logger.info("classify intent=%s confidence=%.2f tenant=%s", intent, confidence, state["tenant_id"])
    return {"intent": intent, "confidence": confidence, "order_number": order_number}


def route_after_classify(state: AgentState) -> str:
    if state.get("confidence", 0.0) < CONFIDENCE_THRESHOLD:
        return "clarify"
    return {
        "knowledge": "retrieve",
        "order": "lookup_order",
        "ticket": "create_ticket",
    }.get(state["intent"], "escalate")
