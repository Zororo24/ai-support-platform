"""Ready-made workflows an admin can add with one click.

Each template names the actions (and their parameters) it relies on. `readiness` tells the
dashboard whether the agent already has them, so the admin is told exactly what is missing
instead of getting a validation error after clicking.
"""

from typing import Any

from app.agents.action_schema import ActionDef

ORDER_NUMBER_PATTERN = "^[A-Z]{3}-[0-9]{3,}$"

TEMPLATES: list[dict[str, Any]] = [
    {
        "id": "ticket_intake",
        "name": "Open a support ticket",
        "description": "Collects the problem, confirms with the customer, then creates a ticket.",
        "requires": {"create_ticket": ["subject", "details"]},
        "workflow": {
            "name": "ticket_intake",
            "description": "The customer wants to open a support ticket or report a problem that needs a person.",
            "start": "details",
            "nodes": {
                "details": {
                    "type": "collect",
                    "fields": [
                        {"name": "subject", "ask": "What's the problem, in a few words?", "max_length": 200},
                        {
                            "name": "details",
                            "ask": "Can you tell me a bit more about it?",
                            "max_length": 2000,
                        },
                    ],
                    "next": "confirm",
                },
                "confirm": {
                    "type": "confirm",
                    "summary": "Open a support ticket: {{vars.subject}}",
                    "yes": "create",
                    "no": "declined",
                },
                "create": {
                    "type": "action",
                    "action": "create_ticket",
                    "args": {"subject": "{{vars.subject}}", "details": "{{vars.details}}"},
                    "save_as": "ticket",
                    "confirmed_by": "confirm",
                    "on_error": "failed",
                    "next": "done",
                },
                "done": {
                    "type": "message",
                    "text": "Done. Your ticket number is {{vars.ticket.ticket_id}}. A person will follow up.",
                },
                "declined": {"type": "message", "text": "No problem, I haven't opened a ticket."},
                "failed": {
                    "type": "message",
                    "text": "Sorry, I couldn't open the ticket just now. Please try again in a moment.",
                },
            },
        },
    },
    {
        "id": "return_request",
        "name": "Return an order",
        "description": "Checks the order is within 30 days of delivery, confirms, then starts the return.",
        "requires": {"get_order": ["order_number"], "create_return": ["order_number", "reason"]},
        "workflow": {
            "name": "return_request",
            "description": "The customer wants to return an item they ordered.",
            "start": "order",
            "nodes": {
                "order": {
                    "type": "collect",
                    "fields": [
                        {
                            "name": "order_number",
                            "ask": "Which order would you like to return? (for example ACM-1002)",
                            "pattern": ORDER_NUMBER_PATTERN,
                            "transform": "upper",
                            "invalid_message": "That doesn't look like an order number.",
                        }
                    ],
                    "next": "lookup",
                },
                "lookup": {
                    "type": "action",
                    "action": "get_order",
                    "args": {"order_number": "{{vars.order_number}}"},
                    "save_as": "order",
                    "on_error": "not_found",
                    "next": "check",
                },
                "check": {
                    "type": "branch",
                    "cases": [{"when": "vars.order.days_since_delivery <= 30", "next": "reason"}],
                    "otherwise": "too_late",
                },
                "reason": {
                    "type": "collect",
                    "fields": [
                        {"name": "reason", "ask": "What's the reason for the return?", "max_length": 500}
                    ],
                    "next": "confirm",
                },
                "confirm": {
                    "type": "confirm",
                    "summary": "Return {{vars.order.item_name}} from order {{vars.order_number}} because: {{vars.reason}}",
                    "yes": "create",
                    "no": "declined",
                },
                "create": {
                    "type": "action",
                    "action": "create_return",
                    "args": {"order_number": "{{vars.order_number}}", "reason": "{{vars.reason}}"},
                    "save_as": "ret",
                    "confirmed_by": "confirm",
                    "on_error": "failed",
                    "next": "done",
                },
                "done": {
                    "type": "message",
                    "text": "Your return is set up (reference {{vars.ret.return_id}}). Your label: {{vars.ret.label_url}}",
                },
                "declined": {"type": "message", "text": "Okay, I haven't started a return."},
                "not_found": {
                    "type": "message",
                    "text": "I couldn't find that order on your account. Please check the number and try again.",
                },
                "failed": {
                    "type": "message",
                    "text": "Sorry, I couldn't start the return just now. Please try again shortly.",
                },
                "too_late": {
                    "type": "message",
                    "text": "That order is outside our 30-day return window, so I'll pass this to a person.",
                    "next": "escalate",
                },
                "escalate": {
                    "type": "handoff",
                    "reason": "Return requested outside the 30-day window",
                    "summary": "Order {{vars.order_number}} was delivered {{vars.order.days_since_delivery}} days ago.",
                },
            },
        },
    },
    {
        "id": "order_status",
        "name": "Check an order",
        "description": "Asks for the order number and tells the customer where it stands.",
        "requires": {"get_order": ["order_number"]},
        "workflow": {
            "name": "order_status",
            "description": "The customer wants to know the status of an order.",
            "start": "order",
            "nodes": {
                "order": {
                    "type": "collect",
                    "fields": [
                        {
                            "name": "order_number",
                            "ask": "What's the order number? (for example ACM-1002)",
                            "pattern": ORDER_NUMBER_PATTERN,
                            "transform": "upper",
                            "invalid_message": "That doesn't look like an order number.",
                        }
                    ],
                    "next": "lookup",
                },
                "lookup": {
                    "type": "action",
                    "action": "get_order",
                    "args": {"order_number": "{{vars.order_number}}"},
                    "save_as": "order",
                    "on_error": "not_found",
                    "next": "answer",
                },
                "answer": {
                    "type": "message",
                    "text": "Order {{vars.order_number}} ({{vars.order.item_name}}) is {{vars.order.status}}.",
                },
                "not_found": {
                    "type": "message",
                    "text": "I couldn't find that order on your account. Please check the number.",
                },
            },
        },
    },
]


def readiness(template: dict[str, Any], actions: list[ActionDef]) -> list[str]:
    """What the agent still lacks for this template (empty list means it can be added)."""
    by_name = {a.name: a for a in actions}
    missing: list[str] = []
    for action_name, params in template["requires"].items():
        action = by_name.get(action_name)
        if action is None:
            missing.append(f"an action called '{action_name}'")
            continue
        for param in params:
            if param not in action.parameters:
                missing.append(f"'{action_name}' needs a parameter called '{param}'")
    return missing
