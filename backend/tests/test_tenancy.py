from uuid import uuid4


def test_acme_can_read_own_order(api, acme_user) -> None:
    response = api.get("/orders/ACM-1001", headers=acme_user)
    assert response.status_code == 200
    assert response.json()["status"] == "shipped"


def test_order_lookup_is_case_insensitive(api, acme_user) -> None:
    assert api.get("/orders/acm-1001", headers=acme_user).status_code == 200


def test_acme_cannot_read_globex_order(api, acme_user, globex_user) -> None:
    assert api.get("/orders/GLX-2001", headers=acme_user).status_code == 404
    assert api.get("/orders/GLX-2001", headers=globex_user).status_code == 200


def test_ticket_create_and_tenant_isolation(api, acme_user, acme_admin, globex_user, globex_admin) -> None:
    created = api.post("/tickets", headers=acme_user, json={"subject": "Broken", "body": "It broke"})
    assert created.status_code == 201
    ticket_id = created.json()["id"]

    assert api.get(f"/tickets/{ticket_id}", headers=acme_user).status_code == 200
    # Same-tenant admin sees it; another tenant (even an admin) does not.
    assert api.get(f"/tickets/{ticket_id}", headers=acme_admin).status_code == 200
    assert api.get(f"/tickets/{ticket_id}", headers=globex_admin).status_code == 404
    assert api.get(f"/tickets/{ticket_id}", headers=globex_user).status_code == 404
    assert api.get("/tickets", headers=globex_admin).json()["items"] == []


def test_customer_sees_only_own_tickets_admin_sees_tenant(api, acme_user, acme_admin) -> None:
    api.post("/tickets", headers=acme_user, json={"subject": "Mine", "body": "x"})
    api.post("/tickets", headers=acme_admin, json={"subject": "Theirs", "body": "y"})
    assert [t["subject"] for t in api.get("/tickets", headers=acme_user).json()["items"]] == ["Mine"]
    assert len(api.get("/tickets", headers=acme_admin).json()["items"]) == 2


def test_unknown_ticket_404(api, acme_user) -> None:
    assert api.get(f"/tickets/{uuid4()}", headers=acme_user).status_code == 404


def test_unauthenticated_requests_rejected(api) -> None:
    assert api.get("/orders/ACM-1001").status_code == 401
    assert api.get("/tickets").status_code == 401
