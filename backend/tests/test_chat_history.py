def send(api, headers, text, conversation_id=None):
    return api.post(
        "/chat/messages", headers=headers, json={"content": text, "conversation_id": conversation_id}
    ).json()


def test_cards_and_citations_survive_reload(api, acme_user) -> None:
    order = send(api, acme_user, "Where is order ACM-1001?")
    conv = order["conversation_id"]
    send(api, acme_user, "I need a refund exception", conv)
    send(api, acme_user, "What is the return window for returns?", conv)

    messages = api.get(f"/conversations/{conv}/messages", headers=acme_user).json()
    assistant = [m for m in messages if m["role"] == "assistant"]
    assert assistant[0]["order"]["order_number"] == "ACM-1001"
    assert assistant[1]["ticket"]["status"] == "open" and assistant[1]["ticket"]["id"]
    assert assistant[2]["citations"] and assistant[2]["citations"][0]["title"] == "Acme Return Policy"
    assert messages[0]["role"] == "user" and messages[0]["ticket"] is None


def test_conversation_list_has_titles_ordered_by_recent_activity(api, acme_user) -> None:
    first = send(api, acme_user, "What is the return window for returns?")["conversation_id"]
    second = send(api, acme_user, "Where is order ACM-1001?")["conversation_id"]
    listing = api.get("/conversations", headers=acme_user).json()
    assert [c["id"] for c in listing] == [second, first]
    assert listing[0]["title"] == "Where is order ACM-1001?"
    send(api, acme_user, "and the shipping speeds?", first)  # activity bumps it to the top
    assert api.get("/conversations", headers=acme_user).json()[0]["id"] == first


def test_long_titles_are_truncated(api, acme_user) -> None:
    send(api, acme_user, "word " * 60)
    title = api.get("/conversations", headers=acme_user).json()[0]["title"]
    assert len(title) <= 61 and title.endswith("…")


def test_feedback_roundtrip_and_clear(api, acme_user) -> None:
    reply = send(api, acme_user, "What is the return window for returns?")
    url = f"/messages/{reply['message_id']}/feedback"
    assert api.put(url, headers=acme_user, json={"value": -1}).json()["feedback"] == -1
    stored = api.get(f"/conversations/{reply['conversation_id']}/messages", headers=acme_user).json()
    assert stored[-1]["feedback"] == -1
    assert api.put(url, headers=acme_user, json={"value": 1}).json()["feedback"] == 1
    assert api.put(url, headers=acme_user, json={"value": None}).json()["feedback"] is None
    assert api.put(url, headers=acme_user, json={"value": 5}).status_code == 422


def test_feedback_only_on_own_assistant_messages(api, acme_user, acme_admin, globex_user) -> None:
    reply = send(api, acme_user, "What is the return window for returns?")
    url = f"/messages/{reply['message_id']}/feedback"
    for other in (acme_admin, globex_user):
        assert api.put(url, headers=other, json={"value": 1}).status_code == 404
    conv = reply["conversation_id"]
    user_msg = api.get(f"/conversations/{conv}/messages", headers=acme_user).json()[0]
    assert user_msg["role"] == "user"
    assert (
        api.put(f"/messages/{user_msg['id']}/feedback", headers=acme_user, json={"value": 1}).status_code
        == 404
    )
