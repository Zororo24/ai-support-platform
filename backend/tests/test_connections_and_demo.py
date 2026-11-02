"""Connections (encrypted credentials, SSRF checks), the action test endpoint, the demo company
API, and an end-to-end run of the seeded Store assistant over real HTTP."""

import json

import pytest
from sqlalchemy import select

from app.agents import executor
from app.core import ssrf
from app.core.config import get_settings
from app.db.models import Connection, DemoReturn, DemoTicket
from app.db.session import get_session_factory
from tests.company import BASE_URL, SECRET, make_store_bot, store_actions


def make_connection(api, headers, **overrides):
    body = {"name": "Acme API", "base_url": BASE_URL, "auth_type": "bearer", "secret": SECRET, **overrides}
    return api.post("/admin/connections", headers=headers, json=body)


# ---------- connections ----------


def test_secret_is_encrypted_and_never_returned(api, acme_admin) -> None:
    r = make_connection(api, acme_admin)
    assert r.status_code == 201
    body = r.json()
    assert body["has_secret"] is True and body["secret_hint"] == "…cret" and "secret" not in body
    assert SECRET not in json.dumps(api.get("/admin/connections", headers=acme_admin).json())
    with get_session_factory()() as db:
        row = db.scalars(select(Connection)).one()
        assert SECRET not in (row.secret_encrypted or "") and row.secret_encrypted.startswith("gAAAA")


@pytest.mark.parametrize(
    "overrides,message",
    [
        ({"base_url": "http://api.acme.test/v1"}, "https"),
        ({"base_url": "https://169.254.169.254/latest"}, "publicly reachable"),
        ({"base_url": "https://127.0.0.1/x"}, "publicly reachable"),
        ({"base_url": "https://unresolvable.test/x"}, "cannot resolve"),
        ({"base_url": "https://u:p@api.acme.test/x"}, "credentials"),
        ({"base_url": "https://api.acme.test/x?key=1"}, "query"),
        ({"auth_type": "header"}, "auth_header_name"),
        ({"auth_type": "header", "auth_header_name": "Host", "secret": "x"}, "cannot be used"),
        ({"auth_type": "basic", "secret": "nocolon"}, "username:password"),
        ({"auth_type": "bearer", "secret": None}, "secret is required"),
    ],
)
def test_unsafe_or_incomplete_connections_are_rejected(api, acme_admin, overrides, message) -> None:
    r = make_connection(api, acme_admin, **overrides)
    assert r.status_code == 422 and message in r.json()["detail"], r.text


def test_duplicate_names_conflict(api, acme_admin, globex_admin) -> None:
    assert make_connection(api, acme_admin).status_code == 201
    assert make_connection(api, acme_admin, name="acme api").status_code == 409
    assert (
        make_connection(api, globex_admin, base_url="https://api.globex.test/v1").status_code == 201
    )  # per tenant


def test_updating_a_connection(api, acme_admin) -> None:
    conn = make_connection(api, acme_admin).json()
    url = f"/admin/connections/{conn['id']}"
    renamed = api.patch(url, headers=acme_admin, json={"name": "Orders system"}).json()
    assert renamed["name"] == "Orders system" and renamed["secret_hint"] == "…cret"  # secret untouched
    rotated = api.patch(url, headers=acme_admin, json={"secret": "brand-new-secret-7777"}).json()
    assert rotated["secret_hint"] == "…7777"
    none = api.patch(url, headers=acme_admin, json={"auth_type": "none"}).json()
    assert none["has_secret"] is False and none["secret_hint"] is None
    assert (
        api.patch(url, headers=acme_admin, json={"auth_type": "bearer"}).status_code == 422
    )  # needs a secret
    assert api.patch(url, headers=acme_admin, json={"base_url": "https://10.0.0.1/x"}).status_code == 422


def test_connection_in_use_cannot_be_deleted(api, acme_admin, company) -> None:
    make_store_bot(api, acme_admin)
    conn = api.get("/admin/connections", headers=acme_admin).json()[0]
    assert conn["used_by"] == ["Store bot"]
    r = api.delete(f"/admin/connections/{conn['id']}", headers=acme_admin)
    assert r.status_code == 409 and "Store bot" in r.json()["detail"]
    spare = make_connection(api, acme_admin, name="Spare").json()
    assert api.delete(f"/admin/connections/{spare['id']}", headers=acme_admin).status_code == 204


def test_connections_are_admin_only_and_tenant_scoped_and_audited(
    api, acme_user, acme_admin, globex_admin
) -> None:
    conn = make_connection(api, acme_admin).json()
    url = f"/admin/connections/{conn['id']}"
    assert api.get("/admin/connections", headers=acme_user).status_code == 403
    assert api.get("/admin/connections", headers=globex_admin).json() == []
    assert api.patch(url, headers=globex_admin, json={"name": "x"}).status_code == 404
    assert api.delete(url, headers=globex_admin).status_code == 404
    api.patch(url, headers=acme_admin, json={"secret": "another-secret-0000"})
    entries = api.get("/admin/audit?action=connection.", headers=acme_admin).json()["items"]
    assert {e["action"] for e in entries} == {"connection.create", "connection.update"}
    dumped = json.dumps(entries)
    assert SECRET not in dumped and "another-secret" not in dumped


# ---------- action test endpoint ----------


def test_action_test_shows_the_request_without_credentials(api, acme_admin, company) -> None:
    bot = make_store_bot(api, acme_admin)
    url = f"/admin/agents/{bot.id}/actions/get_order/test"
    r = api.post(
        url,
        headers=acme_admin,
        json={"arguments": {"order_number": "ACM-1002"}, "context": {"customer_id": "cust_1"}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["request"]["method"] == "GET" and body["request"]["url"].endswith("/v1/orders/ACM-1002")
    assert body["request"]["query"] == {"customer_id": "cust_1"}
    assert body["request"]["headers"]["Authorization"] == "[redacted]" and SECRET not in json.dumps(body)
    assert body["result"]["ok"] is True and body["model_view"]["data"]["status"] == "delivered"
    assert len(company.calls) == 1


def test_action_test_will_not_write_by_accident(api, acme_admin, company) -> None:
    bot = make_store_bot(api, acme_admin)
    url = f"/admin/agents/{bot.id}/actions/create_ticket/test"
    payload = {"arguments": {"subject": "x"}, "context": {"customer_id": "cust_1"}}
    refused = api.post(url, headers=acme_admin, json=payload)
    assert (
        refused.status_code == 422 and "send for real" in refused.json()["detail"] and company.posts() == []
    )
    mocked = api.post(url, headers=acme_admin, json={**payload, "mock": True}).json()
    assert (
        mocked["result"]["mock"] is True
        and mocked["model_view"]["data"]["ticket_id"] == "T-MOCK"
        and company.calls == []
    )
    real = api.post(url, headers=acme_admin, json={**payload, "send_for_real": True}).json()
    assert real["result"]["ok"] is True and len(company.posts()) == 1


def test_action_test_reports_policy_refusals_and_bad_input(api, acme_admin, company) -> None:
    bot = make_store_bot(api, acme_admin)
    base = f"/admin/agents/{bot.id}/actions"
    big = api.post(
        f"{base}/create_return/test",
        headers=acme_admin,
        json={
            "arguments": {"order_number": "A", "amount_cents": 99999},
            "context": {"customer_id": "c"},
            "send_for_real": True,
        },
    ).json()
    assert big["refused"]["code"] == "guard_refused" and company.calls == []
    bad = api.post(
        f"{base}/get_order/test",
        headers=acme_admin,
        json={"arguments": {"order_number": "nope"}, "context": {"customer_id": "c"}},
    ).json()
    assert bad["refused"]["code"] == "invalid_arguments"
    no_ctx = api.post(
        f"{base}/get_order/test", headers=acme_admin, json={"arguments": {"order_number": "ACM-1002"}}
    ).json()
    assert no_ctx["refused"]["code"] == "missing_context"
    assert api.post(f"{base}/ghost/test", headers=acme_admin, json={}).status_code == 404


def test_action_test_is_tenant_scoped(api, acme_admin, globex_admin, company) -> None:
    bot = make_store_bot(api, acme_admin)
    r = api.post(f"/admin/agents/{bot.id}/actions/get_order/test", headers=globex_admin, json={})
    assert r.status_code == 404 and company.calls == []


# ---------- the retrieval tester can mirror one agent ----------


def test_retrieval_tester_can_use_an_agents_knowledge_settings(api, acme_admin) -> None:
    from tests.test_agents_platform import default_agent, new_agent

    cid = api.post("/admin/collections", headers=acme_admin, json={"name": "Gadgets"}).json()["id"]
    api.post(
        "/admin/documents",
        headers=acme_admin,
        json={"title": "Zorblax", "content": "The zorblax gadget is blue.", "collection_id": cid},
    )
    query = {"query": "zorblax gadget colour"}
    everything = api.post("/admin/retrieval/test", headers=acme_admin, json=query).json()
    assert "Zorblax" in [h["title"] for h in everything["hits"]]

    support = default_agent(api, acme_admin)  # attached to the default collection only
    scoped = api.post(
        "/admin/retrieval/test", headers=acme_admin, json={**query, "agent_id": support["id"]}
    ).json()
    assert "Zorblax" not in [h["title"] for h in scoped["hits"]]

    gadget_agent = new_agent(api, acme_admin, "Gadgets", template="blank")
    config = gadget_agent["draft_config"]
    config["knowledge"]["collection_ids"] = [cid]
    api.put(f"/admin/agents/{gadget_agent['id']}/draft", headers=acme_admin, json=config)
    only = api.post(
        "/admin/retrieval/test", headers=acme_admin, json={**query, "agent_id": gadget_agent["id"]}
    ).json()
    assert [h["title"] for h in only["hits"]] == ["Zorblax"]
    assert (
        api.post(
            "/admin/retrieval/test",
            headers=acme_admin,
            json={**query, "agent_id": "00000000-0000-0000-0000-000000000000"},
        ).status_code
        == 404
    )


# ---------- the demo company API ----------


def demo(api, tenant, **extra):
    return {"Authorization": f"Bearer demo-{tenant}-key", **extra}


@pytest.fixture
def demo_data(api):
    """The seeded database already has the demo orders (see seed/seed.py)."""
    return api


def test_demo_company_requires_its_own_key(demo_data) -> None:
    api = demo_data
    assert api.get("/demo-company/orders/ACM-1002?customer_id=cust_acme_1").status_code == 401
    assert (
        api.get("/demo-company/orders/ACM-1002", headers={"Authorization": "Bearer nope"}).status_code == 401
    )
    assert (
        api.get(
            "/demo-company/orders/ACM-1002", headers={"Authorization": "Bearer demo-nobody-key"}
        ).status_code
        == 401
    )


def test_demo_company_scopes_orders_to_tenant_and_customer(demo_data) -> None:
    api = demo_data
    ok = api.get("/demo-company/orders/ACM-1002?customer_id=cust_acme_1", headers=demo(api, "acme"))
    assert ok.status_code == 200 and ok.json()["item_name"] == "Noise-cancelling headphones"
    assert ok.json()["days_since_delivery"] == 12
    assert (
        api.get(
            "/demo-company/orders/ACM-1002?customer_id=cust_acme_2", headers=demo(api, "acme")
        ).status_code
        == 404
    )
    assert (
        api.get("/demo-company/orders/ACM-1002", headers=demo(api, "acme")).status_code == 404
    )  # no customer given
    assert (
        api.get(
            "/demo-company/orders/GLX-2001?customer_id=cust_globex_1", headers=demo(api, "acme")
        ).status_code
        == 404
    )


def test_demo_company_tickets_and_returns(demo_data) -> None:
    api = demo_data
    headers = demo(api, "acme")
    t = api.post(
        "/demo-company/tickets",
        headers=headers,
        json={"subject": "Hi", "body": "Help", "customer_id": "cust_acme_1"},
    )
    assert t.status_code == 201 and t.json()["ticket_id"].startswith("TCK-")
    assert (
        len(api.get("/demo-company/tickets?customer_id=cust_acme_1", headers=headers).json()["tickets"]) == 1
    )
    assert (
        api.get("/demo-company/tickets?customer_id=cust_acme_1", headers=demo(api, "globex")).json()[
            "tickets"
        ]
        == []
    )

    r = api.post(
        "/demo-company/returns",
        headers=headers,
        json={"order_number": "ACM-1002", "reason": "broken", "customer_id": "cust_acme_1"},
    )
    assert r.status_code == 201 and r.json()["return_id"].startswith("RET-")
    not_delivered = api.post(
        "/demo-company/returns",
        headers=headers,
        json={"order_number": "ACM-1001", "reason": "x", "customer_id": "cust_acme_1"},
    )
    assert not_delivered.status_code == 409
    foreign = api.post(
        "/demo-company/returns",
        headers=headers,
        json={"order_number": "ACM-1002", "reason": "x", "customer_id": "cust_acme_2"},
    )
    assert foreign.status_code == 404


# ---------- the seeded Store assistant, end to end over real HTTP ----------


@pytest.fixture
def store_assistant(api, monkeypatch):
    """Seed the demo agent against the app's own /demo-company routes (loopback, dev switch on)."""
    from seed.seed import seed_demo_agent

    monkeypatch.setattr(get_settings(), "allow_private_connections", True)
    monkeypatch.setattr(ssrf, "resolve_host", lambda host: ["127.0.0.1"])
    monkeypatch.setenv("DEMO_COMPANY_URL", "http://testserver/demo-company")
    executor.set_client_override(api)  # the TestClient *is* an httpx client bound to the app
    with get_session_factory()() as db:
        from app.db.models import Tenant

        tenants = {t.slug: t for t in db.scalars(select(Tenant))}
        assert seed_demo_agent(db, tenants) == 2
    yield
    executor.set_client_override(None)


def test_seeded_store_assistant_checks_an_order_and_starts_a_return(
    api, acme_admin, store_assistant, fake_llm
) -> None:
    from tests.test_public_api import make_bot

    agent = next(
        a for a in api.get("/admin/agents", headers=acme_admin).json() if a["name"] == "Store assistant"
    )
    assert agent["published_version"] == 1
    bot = make_bot(api, acme_admin, agent)
    customer = {"id": "web-1", "attributes": {"customer_id": "cust_acme_1"}}

    fake_llm.script(
        {"type": "call_action", "name": "get_order", "arguments": {"order_number": "ACM-1002"}},
        {
            "type": "call_action",
            "name": "create_return",
            "arguments": {"order_number": "ACM-1002", "reason": "stopped working"},
        },
    )
    first = bot.send(
        "I want to return ACM-1002, it stopped working", end_user=customer, session_id="web-1"
    ).json()
    assert first["status"] == "needs_confirmation"
    assert "Start a return for order ACM-1002 (reason: stopped working)" in first["reply"]
    with get_session_factory()() as db:
        assert db.scalars(select(DemoReturn)).first() is None  # not yet

    fake_llm.script({"type": "reply", "message": "Your return is started; the label is in your email."})
    done = bot.send("yes", end_user=customer, session_id="web-1").json()
    assert done["status"] == "completed" and [a["name"] for a in done["actions_taken"]] == [
        "get_order",
        "create_return",
    ]
    with get_session_factory()() as db:
        (ret,) = db.scalars(select(DemoReturn)).all()
        assert (ret.order_number, ret.customer_ref, ret.reason) == (
            "ACM-1002",
            "cust_acme_1",
            "stopped working",
        )


def test_seeded_store_assistant_cannot_touch_another_customers_order(
    api, acme_admin, store_assistant, fake_llm
) -> None:
    from tests.test_public_api import make_bot

    agent = next(
        a for a in api.get("/admin/agents", headers=acme_admin).json() if a["name"] == "Store assistant"
    )
    bot = make_bot(api, acme_admin, agent)
    fake_llm.script(
        {
            "type": "call_action",
            "name": "get_order",
            "arguments": {"order_number": "ACM-2001"},
        },  # belongs to cust_acme_2
        {"type": "reply", "message": "I can't find that order."},
    )
    r = bot.send(
        "where is ACM-2001", end_user={"id": "u", "attributes": {"customer_id": "cust_acme_1"}}
    ).json()
    assert r["actions_taken"] == [{"name": "get_order", "effect": "read", "ok": False, "error": "http_error"}]
    with get_session_factory()() as db:
        assert db.scalars(select(DemoTicket)).first() is None


def test_demo_agent_actions_are_valid_definitions() -> None:
    from seed.seed import demo_actions

    from app.agents.action_schema import ActionDef

    actions = [ActionDef.model_validate(a) for a in demo_actions("00000000-0000-0000-0000-000000000001")]
    assert [a.name for a in actions] == ["get_order", "create_ticket", "create_return"]
    assert store_actions  # the shared test actions stay importable
