from uuid import uuid4


def make_ticket(api, headers, subject="Broken thing", body="It broke", **extra):
    response = api.post("/tickets", headers=headers, json={"subject": subject, "body": body, **extra})
    assert response.status_code == 201, response.text
    return response.json()


def admin_id(api, headers):
    return api.get("/auth/me", headers=headers).json()["id"]


def test_ticket_out_has_requester_priority_and_defaults(api, acme_user) -> None:
    ticket = make_ticket(api, acme_user)
    assert ticket["requester_email"] == "acme.user@example.com"
    assert ticket["priority"] == "normal" and ticket["status"] == "open"
    assert ticket["assignee_id"] is None and ticket["created_at"]


def test_admin_triages_ticket_customer_cannot(api, acme_user, acme_admin) -> None:
    ticket = make_ticket(api, acme_user)
    me = admin_id(api, acme_admin)
    patched = api.patch(
        f"/tickets/{ticket['id']}",
        headers=acme_admin,
        json={"status": "pending", "priority": "high", "assignee_id": me},
    )
    assert patched.status_code == 200
    body = patched.json()
    assert (body["status"], body["priority"], body["assignee_email"]) == (
        "pending",
        "high",
        "acme.admin@example.com",
    )
    assert (
        api.patch(f"/tickets/{ticket['id']}", headers=acme_user, json={"status": "closed"}).status_code == 403
    )


def test_patch_can_unassign_and_omitting_assignee_leaves_it(api, acme_user, acme_admin) -> None:
    ticket = make_ticket(api, acme_user)
    me = admin_id(api, acme_admin)
    url = f"/tickets/{ticket['id']}"
    api.patch(url, headers=acme_admin, json={"assignee_id": me})
    kept = api.patch(url, headers=acme_admin, json={"priority": "low"}).json()
    assert kept["assignee_id"] == me  # not mentioned, so untouched
    cleared = api.patch(url, headers=acme_admin, json={"assignee_id": None}).json()
    assert cleared["assignee_id"] is None


def test_assignee_must_be_active_admin_in_same_tenant(api, acme_user, acme_admin, globex_admin) -> None:
    ticket = make_ticket(api, acme_user)
    url = f"/tickets/{ticket['id']}"
    other_tenant_admin = admin_id(api, globex_admin)
    customer = api.get("/auth/me", headers=acme_user).json()["id"]
    for bad in (other_tenant_admin, customer, str(uuid4())):
        assert api.patch(url, headers=acme_admin, json={"assignee_id": bad}).status_code == 422


def test_other_tenant_admin_cannot_triage_or_comment(api, acme_user, globex_admin) -> None:
    ticket = make_ticket(api, acme_user)
    url = f"/tickets/{ticket['id']}"
    assert api.patch(url, headers=globex_admin, json={"status": "closed"}).status_code == 404
    assert api.post(f"{url}/comments", headers=globex_admin, json={"body": "hi"}).status_code == 404


def test_comment_thread_between_customer_and_admin(api, acme_user, acme_admin) -> None:
    ticket = make_ticket(api, acme_user)
    url = f"/tickets/{ticket['id']}"
    assert (
        api.post(f"{url}/comments", headers=acme_admin, json={"body": "Can you send a photo?"}).status_code
        == 201
    )
    assert api.post(f"{url}/comments", headers=acme_user, json={"body": "Here it is"}).status_code == 201
    thread = api.get(url, headers=acme_user).json()["comments"]
    assert [(c["author_role"], c["body"]) for c in thread] == [
        ("admin", "Can you send a photo?"),
        ("customer", "Here it is"),
    ]


def test_customer_reply_reopens_closed_or_pending_ticket(api, acme_user, acme_admin) -> None:
    ticket = make_ticket(api, acme_user)
    url = f"/tickets/{ticket['id']}"
    api.patch(url, headers=acme_admin, json={"status": "closed"})
    api.post(f"{url}/comments", headers=acme_user, json={"body": "Still broken"})
    assert api.get(url, headers=acme_user).json()["status"] == "open"
    api.patch(url, headers=acme_admin, json={"status": "pending"})
    api.post(f"{url}/comments", headers=acme_admin, json={"body": "Any update?"})
    assert api.get(url, headers=acme_user).json()["status"] == "pending"  # admin replies don't change it


def test_customer_cannot_comment_on_someone_elses_ticket(api, acme_admin, acme_user) -> None:
    admins_ticket = make_ticket(api, acme_admin, subject="Admin's own")
    resp = api.post(f"/tickets/{admins_ticket['id']}/comments", headers=acme_user, json={"body": "hi"})
    assert resp.status_code == 404


def test_blank_comment_rejected(api, acme_user) -> None:
    ticket = make_ticket(api, acme_user)
    assert (
        api.post(f"/tickets/{ticket['id']}/comments", headers=acme_user, json={"body": "   "}).status_code
        == 422
    )


def test_queue_filters(api, acme_user, acme_admin) -> None:
    me = admin_id(api, acme_admin)
    a = make_ticket(api, acme_user, subject="Refund please", priority="high")
    b = make_ticket(api, acme_user, subject="Broken zipper")
    api.patch(f"/tickets/{a['id']}", headers=acme_admin, json={"assignee_id": me, "status": "pending"})

    def ids(query):
        return {t["id"] for t in api.get(f"/tickets?{query}", headers=acme_admin).json()["items"]}

    assert ids("status=pending") == {a["id"]}
    assert ids("priority=high") == {a["id"]}
    assert ids("assignee=me") == {a["id"]}
    assert ids("assignee=unassigned") == {b["id"]}
    assert ids(f"assignee={me}") == {a["id"]}
    assert ids("q=zipper") == {b["id"]}
    assert api.get("/tickets?status=nonsense", headers=acme_admin).status_code == 422
    assert api.get("/tickets?assignee=nonsense", headers=acme_admin).status_code == 422
    page = api.get("/tickets?limit=1", headers=acme_admin).json()
    assert len(page["items"]) == 1 and page["total"] == 2


def test_customer_filters_never_widen_visibility(api, acme_user, acme_admin) -> None:
    make_ticket(api, acme_admin, subject="Admin ticket")
    mine = make_ticket(api, acme_user)
    items = api.get("/tickets?assignee=unassigned", headers=acme_user).json()["items"]
    assert [t["id"] for t in items] == [mine["id"]]


def test_bulk_close_only_touches_own_tenant(api, acme_user, acme_admin, globex_user, globex_admin) -> None:
    mine = [make_ticket(api, acme_user, subject=f"t{i}")["id"] for i in range(3)]
    foreign = make_ticket(api, globex_user)["id"]
    resp = api.post(
        "/tickets/bulk-status", headers=acme_admin, json={"ids": mine + [foreign], "status": "closed"}
    )
    assert resp.json() == {"updated": 3}
    assert api.get(f"/tickets/{foreign}", headers=globex_admin).json()["status"] == "open"
    assert (
        api.post(
            "/tickets/bulk-status", headers=acme_user, json={"ids": mine, "status": "closed"}
        ).status_code
        == 403
    )
    assert (
        api.post("/tickets/bulk-status", headers=acme_admin, json={"ids": [], "status": "closed"}).status_code
        == 422
    )


def test_chat_created_ticket_links_to_its_conversation(api, acme_user) -> None:
    body = api.post("/chat/messages", headers=acme_user, json={"content": "I need a refund exception"}).json()
    ticket = api.get(f"/tickets/{body['ticket']['id']}", headers=acme_user).json()
    assert ticket["conversation_id"] == body["conversation_id"]
