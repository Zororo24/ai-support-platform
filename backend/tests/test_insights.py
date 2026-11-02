def send(api, headers, text, conversation_id=None):
    return api.post(
        "/chat/messages", headers=headers, json={"content": text, "conversation_id": conversation_id}
    ).json()


GOOD = "What is the return window for returns?"
UNANSWERABLE = "zzzqx plugh xyzzy"


# ---------- explainability ----------


def test_admin_sees_how_the_answer_was_produced_customer_does_not(api, acme_user, acme_admin) -> None:
    as_admin = send(api, acme_admin, GOOD)
    meta = as_admin["meta"]
    assert meta["intent"] == "knowledge" and meta["outcome"] == "answered"
    assert meta["question"] == GOOD
    assert meta["llm_calls"] >= 3  # embed query + classify + answer
    assert meta["total_tokens"] > 0 and meta["latency_ms"] >= 0
    assert meta["retrieved"] and meta["retrieved"][0]["title"] == "Acme Return Policy"
    assert meta["cited"]
    assert send(api, acme_user, GOOD)["meta"] == {}  # customers never get internals

    stored_admin = api.get(
        f"/conversations/{as_admin['conversation_id']}/messages", headers=acme_admin
    ).json()
    assert stored_admin[-1]["meta"]["outcome"] == "answered"


def test_outcomes_are_recorded_per_path(api, acme_admin) -> None:
    cases = {
        GOOD: "answered",
        UNANSWERABLE: "refused",
        "Where is order ACM-1001?": "order_found",
        "Where is order ACM-9999?": "order_not_found",
        "Where is my order?": "order_needs_number",
        "I need a refund exception": "ticket_created",
        "hello": "other",
        "hmm": "clarify",
    }
    for text, outcome in cases.items():
        assert send(api, acme_admin, text)["meta"]["outcome"] == outcome, text


# ---------- knowledge gaps ----------


def test_gaps_group_refusals_and_thumbs_down(api, acme_user, acme_admin) -> None:
    send(api, acme_user, UNANSWERABLE)
    send(api, acme_user, "  ZZZQX   plugh xyzzy ")  # same question, different casing/spacing
    bad = send(api, acme_user, GOOD)
    api.put(f"/messages/{bad['message_id']}/feedback", headers=acme_user, json={"value": -1})
    good = send(api, acme_user, "How fast is express shipping?")
    api.put(f"/messages/{good['message_id']}/feedback", headers=acme_user, json={"value": 1})
    send(api, acme_user, "I need a refund exception")  # a ticket request is not a knowledge gap

    gaps = api.get("/admin/insights/gaps", headers=acme_admin).json()
    by_question = {g["question"].strip().lower(): g for g in gaps}
    refused = (
        by_question["zzzqx   plugh xyzzy"]
        if "zzzqx   plugh xyzzy" in by_question
        else next(g for g in gaps if "zzzqx" in g["question"].lower())
    )
    assert refused["count"] == 2 and refused["refused"] == 2
    down = next(g for g in gaps if g["question"] == GOOD)
    assert down["thumbs_down"] == 1 and down["refused"] == 0
    assert not any("express" in g["question"] for g in gaps)  # helpful answer
    assert not any("refund" in g["question"] for g in gaps)
    assert gaps[0]["count"] >= gaps[-1]["count"]  # most frequent first


def test_gaps_are_tenant_scoped_and_admin_only(api, acme_user, acme_admin, globex_admin) -> None:
    send(api, acme_user, UNANSWERABLE)
    assert len(api.get("/admin/insights/gaps", headers=acme_admin).json()) == 1
    assert api.get("/admin/insights/gaps", headers=globex_admin).json() == []
    assert api.get("/admin/insights/gaps", headers=acme_user).status_code == 403
    assert api.get("/admin/insights/gaps?days=0", headers=acme_admin).status_code == 422


# ---------- dashboard summary ----------


def test_summary_numbers(api, acme_user, acme_admin) -> None:
    a = send(api, acme_user, GOOD)
    send(api, acme_user, UNANSWERABLE, a["conversation_id"])
    send(api, acme_user, "I need a refund exception")  # second conversation
    api.put(f"/messages/{a['message_id']}/feedback", headers=acme_user, json={"value": 1})

    s = api.get("/admin/insights/summary?days=7", headers=acme_admin).json()
    assert s["days"] == 7 and s["conversations"] == 2 and s["replies"] == 3
    assert s["outcomes"] == {"answered": 1, "refused": 1, "ticket_created": 1}
    assert s["answered_rate"] == 0.5
    assert s["tickets_from_chat"] == 1
    assert (s["thumbs_up"], s["thumbs_down"], s["helpful_rate"]) == (1, 0, 1.0)
    assert s["total_tokens"] > 0 and s["avg_latency_ms"] is not None
    assert len(s["series"]) == 7  # continuous axis, quiet days included
    assert s["series"][-1]["messages"] == 3 and s["series"][-1]["conversations"] == 2
    assert sum(p["messages"] for p in s["series"][:-1]) == 0
    assert [g["question"] for g in s["top_gaps"]] == [UNANSWERABLE]


def test_empty_summary_has_null_rates_not_errors(api, acme_admin) -> None:
    s = api.get("/admin/insights/summary", headers=acme_admin).json()
    assert s["replies"] == 0 and s["answered_rate"] is None and s["helpful_rate"] is None
    assert s["avg_latency_ms"] is None and len(s["series"]) == 30


def test_summary_is_tenant_scoped_and_admin_only(api, acme_user, acme_admin, globex_admin) -> None:
    send(api, acme_user, GOOD)
    assert api.get("/admin/insights/summary", headers=globex_admin).json()["replies"] == 0
    assert api.get("/admin/insights/summary", headers=acme_user).status_code == 403
    assert api.get("/admin/insights/summary?days=500", headers=acme_admin).status_code == 422


# ---------- audit log ----------


def actions(api, headers, query=""):
    return [e["action"] for e in api.get(f"/admin/audit{query}", headers=headers).json()["items"]]


def test_admin_actions_are_audited(api, acme_user, acme_admin) -> None:
    created = api.post(
        "/admin/documents", headers=acme_admin, json={"title": "T", "content": "Some text."}
    ).json()
    up = api.post(
        "/admin/documents/upload", headers=acme_admin, files={"file": ("n.txt", b"Notes here.")}
    ).json()
    api.patch(f"/admin/documents/{created['id']}", headers=acme_admin, json={"title": "T2"})
    api.post(f"/admin/documents/{created['id']}/reindex", headers=acme_admin)
    api.delete(f"/admin/documents/{up['id']}", headers=acme_admin)
    target = next(
        u for u in api.get("/admin/users", headers=acme_admin).json() if u["email"] == "acme.user@example.com"
    )
    # Create the ticket first: the password reset below signs this user out everywhere.
    ticket = api.post("/tickets", headers=acme_user, json={"subject": "s", "body": "b"}).json()
    api.patch(
        f"/admin/users/{target['id']}",
        headers=acme_admin,
        json={"role": "admin", "password": "secret-value-123"},
    )
    api.patch(f"/tickets/{ticket['id']}", headers=acme_admin, json={"status": "closed", "priority": "high"})
    api.post("/tickets/bulk-status", headers=acme_admin, json={"ids": [ticket["id"]], "status": "open"})

    seen = actions(api, acme_admin)
    for expected in (
        "document.create",
        "document.upload",
        "document.update",
        "document.reindex",
        "document.delete",
        "user.update",
        "ticket.update",
        "ticket.bulk_status",
    ):
        assert expected in seen, (expected, seen)

    entries = api.get("/admin/audit", headers=acme_admin).json()["items"]
    assert entries[0]["action"] == "ticket.bulk_status"  # newest first
    assert all(e["actor_email"] == "acme.admin@example.com" for e in entries)
    user_update = next(e for e in entries if e["action"] == "user.update")
    assert user_update["detail"]["role"] == {"from": "customer", "to": "admin"}
    assert user_update["detail"]["password_reset"] is True
    assert "secret-value-123" not in str(entries)  # secrets never reach the log
    ticket_update = next(e for e in entries if e["action"] == "ticket.update")
    assert ticket_update["detail"]["status"] == {"from": "open", "to": "closed"}
    assert ticket_update["detail"]["priority"] == {"from": "normal", "to": "high"}


def test_failed_actions_leave_no_entry(api, acme_admin) -> None:
    api.post("/admin/documents/upload", headers=acme_admin, files={"file": ("a.txt", b"Same.")})
    before = len(actions(api, acme_admin))
    assert (
        api.post(
            "/admin/documents/upload", headers=acme_admin, files={"file": ("b.txt", b"Same.")}
        ).status_code
        == 409
    )
    assert (
        api.post("/admin/documents/upload", headers=acme_admin, files={"file": ("x.exe", b"MZ")}).status_code
        == 422
    )
    me = api.get("/auth/me", headers=acme_admin).json()
    assert (
        api.patch(f"/admin/users/{me['id']}", headers=acme_admin, json={"role": "customer"}).status_code
        == 409
    )
    assert len(actions(api, acme_admin)) == before


def test_audit_filter_pagination_and_isolation(api, acme_user, acme_admin, globex_admin) -> None:
    api.post("/admin/documents", headers=acme_admin, json={"title": "T", "content": "x one"})
    api.post("/admin/users", headers=acme_admin, json={"email": "n@example.com", "password": "longenough1"})
    assert actions(api, acme_admin, "?action=user.") == ["user.create"]
    assert actions(api, acme_admin, "?action=document.") == ["document.create"]
    page = api.get("/admin/audit?limit=1", headers=acme_admin).json()
    assert len(page["items"]) == 1 and page["total"] == 2
    assert api.get("/admin/audit", headers=globex_admin).json() == {"items": [], "total": 0}
    assert api.get("/admin/audit", headers=acme_user).status_code == 403
    assert api.get("/admin/audit").status_code == 401
