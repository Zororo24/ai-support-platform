"""A fake company system plus helpers to build an agent that uses it.

`FakeCompany.handler` stands in for a customer's own API behind httpx's MockTransport, so tests
exercise the real executor (templating, SSRF checks, IP pinning, auth, redaction) with no network.
"""

import json
from typing import Any

import httpx

from tests.test_agents_platform import edit_and_publish, new_agent
from tests.test_public_api import Bot, make_bot

SECRET = "acme-secret"
BASE_URL = "https://api.acme.test/v1"

ORDERS = {
    "ACM-1002": {
        "customer": "cust_1",
        "status": "delivered",
        "item_name": "Headphones",
        "days_since_delivery": 12,
    },
    "ACM-1003": {
        "customer": "cust_1",
        "status": "delivered",
        "item_name": "Phone case",
        "days_since_delivery": 45,
    },
    "ACM-2001": {
        "customer": "cust_2",
        "status": "delivered",
        "item_name": "Desk lamp",
        "days_since_delivery": 5,
    },
}


class FakeCompany:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_status: int | None = None
        self.order_note: str | None = None  # text smuggled into an order (prompt-injection tests)

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append(
            {
                "method": request.method,
                "path": request.url.path,
                "query": dict(request.url.params),
                "body": body,
                "headers": dict(request.headers),
            }
        )
        if request.headers.get("authorization") != f"Bearer {SECRET}":
            return httpx.Response(401, json={"message": "bad key"})
        if self.fail_status:
            return httpx.Response(self.fail_status, json={"message": "the service is having a bad day"})
        path = request.url.path
        if request.method == "GET" and path.startswith("/v1/orders/"):
            order = ORDERS.get(path.rsplit("/", 1)[1])
            if order is None or order["customer"] != request.url.params.get("customer_id"):
                return httpx.Response(404, json={"message": "order not found"})
            data = {
                "order_number": path.rsplit("/", 1)[1],
                **{k: v for k, v in order.items() if k != "customer"},
            }
            if self.order_note:
                data["note"] = self.order_note
            return httpx.Response(200, json=data)
        if request.method == "POST" and path == "/v1/tickets":
            return httpx.Response(201, json={"ticket_id": f"T-{len(self.calls)}", "status": "open"})
        if request.method == "POST" and path == "/v1/returns":
            return httpx.Response(
                201, json={"return_id": "R-1", "label_url": "https://l.test/1", "status": "created"}
            )
        return httpx.Response(404, json={"message": "no such route"})

    def posts(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["method"] == "POST"]


class FakeReceiver:
    """The company's webhook endpoint: records what it was sent and answers with a scripted status."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.statuses: list[int] = []  # answers in order; 200 once the list runs out

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(
            {
                "path": request.url.path,
                "host": request.headers.get("host"),
                "headers": dict(request.headers),
                "body": request.content,
                "json": json.loads(request.content),
            }
        )
        return httpx.Response(self.statuses.pop(0) if self.statuses else 200, json={"ok": True})

    def events(self, event_type: str | None = None) -> list[dict[str, Any]]:
        found = [r["json"] for r in self.requests]
        return [e for e in found if event_type is None or e["type"] == event_type]


def store_actions(connection_id: str) -> list[dict[str, Any]]:
    return [
        {
            "name": "get_order",
            "description": "Look up one of the customer's orders.",
            "connection_id": connection_id,
            "request": {
                "method": "GET",
                "path": "/orders/{{args.order_number}}",
                "query": {"customer_id": "{{context.customer_id}}"},
            },
            "parameters": {
                "order_number": {"type": "string", "required": True, "pattern": "^[A-Z]{3}-[0-9]{3,}$"}
            },
            "response": {
                "expose": ["$.order_number", "$.status", "$.item_name", "$.days_since_delivery", "$.note"]
            },
            "mock": {
                "order_number": "ACM-0000",
                "status": "mocked",
                "item_name": "Mock item",
                "days_since_delivery": 5,
            },
        },
        {
            "name": "create_ticket",
            "description": "Open a support ticket for the customer.",
            "connection_id": connection_id,
            "request": {
                "method": "POST",
                "path": "/tickets",
                "body": {"subject": "{{args.subject}}", "customer_id": "{{context.customer_id}}"},
            },
            "parameters": {"subject": {"type": "string", "required": True, "max_length": 100}},
            "effect": "write",
            "confirm_summary": "Open a support ticket: {{args.subject}}",
            "idempotency_header": "Idempotency-Key",
            "response": {"expose": ["$.ticket_id", "$.status"]},
            "mock": {"ticket_id": "T-MOCK", "status": "open"},
        },
        {
            "name": "create_return",
            "description": "Start a return for a delivered order.",
            "connection_id": connection_id,
            "request": {
                "method": "POST",
                "path": "/returns",
                "body": {
                    "order_number": "{{args.order_number}}",
                    "amount_cents": "{{args.amount_cents}}",
                    "customer_id": "{{context.customer_id}}",
                },
            },
            "parameters": {
                "order_number": {"type": "string", "required": True},
                "amount_cents": {"type": "integer", "required": True, "minimum": 1},
            },
            "effect": "write",
            "guards": [{"when": "args.amount_cents > 20000", "message": "Returns over $200 need a person."}],
            "response": {"expose": ["$.return_id", "$.status"]},
        },
    ]


def make_store_bot(api, admin_headers, **config_overrides) -> Bot:
    """A published agent wired to the fake company through a real Connection."""
    connection = api.post(
        "/admin/connections",
        headers=admin_headers,
        json={"name": "Acme API", "base_url": BASE_URL, "auth_type": "bearer", "secret": SECRET},
    )
    assert connection.status_code == 201, connection.text
    agent = new_agent(api, admin_headers, "Store bot", template="support")

    def configure(config: dict[str, Any]) -> None:
        config["actions"] = store_actions(connection.json()["id"])
        config.update(config_overrides)

    edit_and_publish(api, admin_headers, agent["id"], configure, note="store bot")
    return make_bot(api, admin_headers, agent)


CUSTOMER = {"id": "web-user-1", "attributes": {"customer_id": "cust_1", "plan": "pro"}}


def workflow_actions(connection_id: str) -> list[dict[str, Any]]:
    """Actions shaped like the shipped workflow templates expect (get_order, create_return, create_ticket)."""
    get_order = store_actions(connection_id)[0]
    return [
        get_order,
        {
            "name": "create_return",
            "description": "Start a return for a delivered order.",
            "connection_id": connection_id,
            "request": {
                "method": "POST",
                "path": "/returns",
                "body": {
                    "order_number": "{{args.order_number}}",
                    "reason": "{{args.reason}}",
                    "customer_id": "{{context.customer_id}}",
                },
            },
            "parameters": {
                "order_number": {"type": "string", "required": True},
                "reason": {"type": "string", "required": True},
            },
            "effect": "write",
            "idempotency_header": "Idempotency-Key",
            "response": {"expose": ["$.return_id", "$.label_url", "$.status"]},
            "mock": {"return_id": "R-MOCK", "label_url": "https://l.test/mock", "status": "created"},
        },
        {
            "name": "create_ticket",
            "description": "Open a support ticket for the customer.",
            "connection_id": connection_id,
            "request": {
                "method": "POST",
                "path": "/tickets",
                "body": {
                    "subject": "{{args.subject}}",
                    "body": "{{args.details}}",
                    "customer_id": "{{context.customer_id}}",
                },
            },
            "parameters": {
                "subject": {"type": "string", "required": True},
                "details": {"type": "string", "required": True},
            },
            "effect": "write",
            "response": {"expose": ["$.ticket_id", "$.status"]},
            "mock": {"ticket_id": "T-MOCK", "status": "open"},
        },
    ]


def template_workflow(template_id: str) -> dict[str, Any]:
    import copy

    from app.agents.workflow_templates import TEMPLATES

    return copy.deepcopy(next(t for t in TEMPLATES if t["id"] == template_id)["workflow"])


def make_workflow_bot(api, admin_headers, workflows: list[dict[str, Any]], **config_overrides) -> Bot:
    """A published agent with the three workflow-ready actions and the given workflows."""
    connection = api.post(
        "/admin/connections",
        headers=admin_headers,
        json={"name": "Acme API", "base_url": BASE_URL, "auth_type": "bearer", "secret": SECRET},
    )
    assert connection.status_code == 201, connection.text
    agent = new_agent(api, admin_headers, "Workflow bot", template="support")

    def configure(config: dict[str, Any]) -> None:
        config["actions"] = workflow_actions(connection.json()["id"])
        config["workflows"] = workflows
        config.update(config_overrides)

    edit_and_publish(api, admin_headers, agent["id"], configure, note="workflow bot")
    return make_bot(api, admin_headers, agent)
