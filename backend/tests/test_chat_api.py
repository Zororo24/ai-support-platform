from uuid import uuid4


def test_chat_creates_and_reuses_conversation(api, acme_user) -> None:
    first = api.post("/chat/messages", headers=acme_user, json={"content": "hello"})
    assert first.status_code == 200
    conv = first.json()["conversation_id"]
    second = api.post("/chat/messages", headers=acme_user, json={"content": "again", "conversation_id": conv})
    assert second.json()["conversation_id"] == conv
    messages = api.get(f"/conversations/{conv}/messages", headers=acme_user).json()
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]
    assert [c["id"] for c in api.get("/conversations", headers=acme_user).json()] == [conv]


def test_conversations_are_private(api, acme_user, acme_admin, globex_user) -> None:
    conv = api.post("/chat/messages", headers=acme_user, json={"content": "hi"}).json()["conversation_id"]
    for other in (acme_admin, globex_user):
        assert api.get(f"/conversations/{conv}/messages", headers=other).status_code == 404
        resp = api.post("/chat/messages", headers=other, json={"content": "hijack", "conversation_id": conv})
        assert resp.status_code == 404
        assert api.get("/conversations", headers=other).json() == []


def test_unknown_conversation_404(api, acme_user) -> None:
    resp = api.post(
        "/chat/messages", headers=acme_user, json={"content": "x", "conversation_id": str(uuid4())}
    )
    assert resp.status_code == 404


def test_chat_requires_auth(api) -> None:
    assert api.post("/chat/messages", json={"content": "x"}).status_code == 401
