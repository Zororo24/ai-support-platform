def ask(api, headers, text):
    return api.post("/chat/messages", headers=headers, json={"content": text}).json()


def test_acme_faq_answers_from_acme_docs(api, acme_user) -> None:
    body = ask(api, acme_user, "What is the return window for returns?")
    assert body["citations"], body
    assert {c["title"] for c in body["citations"]} <= {"Acme Return Policy", "Acme Shipping FAQ"}
    assert "30 days" in body["content"]


def test_cross_tenant_question_never_cites_other_tenant(api, acme_user) -> None:
    body = ask(api, acme_user, "Tell me about the Globex Flex Credit program and Globex returns")
    for citation in body["citations"]:
        assert "Globex" not in citation["title"]
    assert "Globex Flex Credit" not in body["content"] or "Acme" in body["content"]
    assert "60 days" not in body["content"]


def test_globex_user_gets_globex_policy(api, globex_user) -> None:
    body = ask(api, globex_user, "What is the return window for returns?")
    assert "60 days" in body["content"]
    assert all("Globex" in c["title"] for c in body["citations"])


def test_unrelated_question_is_refused(api, acme_user) -> None:
    body = ask(api, acme_user, "zzzqx plugh xyzzy")
    assert body["citations"] == []
    assert "couldn't find" in body["content"]


def test_admin_ingest_is_tenant_scoped_and_role_gated(api, acme_user, acme_admin, globex_user) -> None:
    payload = {
        "title": "Acme Warranty",
        "content": "# Acme Warranty\nAll Acme gadgets carry a 2 year warranty.",
    }
    assert api.post("/admin/documents", headers=acme_user, json=payload).status_code == 403
    created = api.post("/admin/documents", headers=acme_admin, json=payload)
    assert created.status_code == 201
    # The background task has finished by the time the client call returns.
    detail = api.get(f"/admin/documents/{created.json()['id']}", headers=acme_admin).json()
    assert detail["status"] == "ready" and detail["chunk_count"] >= 1

    assert "2 year" in ask(api, acme_user, "How long is the gadget warranty?")["content"]
    other = ask(api, globex_user, "How long is the gadget warranty?")
    assert "2 year" not in other["content"]
