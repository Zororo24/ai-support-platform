import json
from unittest.mock import patch

import pytest

from app.agents.nodes import classify as classify_module
from app.agents.nodes.classify import classify, extract_order_number, route_after_classify
from app.core.llm import LLMUnavailable


def ask(api, headers, text, conversation_id=None):
    return api.post(
        "/chat/messages", headers=headers, json={"content": text, "conversation_id": conversation_id}
    ).json()


def chat_override(fake, payload_for):
    """Make the fake LLM answer chat calls with payload_for(messages); embeddings stay normal."""
    original = type(fake).__call__

    def override(path, payload):
        if path == "/chat/completions":
            fake.chat_calls.append(payload["messages"])
            return {"choices": [{"message": {"content": payload_for(payload["messages"])}}]}
        fake.override = None
        try:
            return original(fake, path, payload)
        finally:
            fake.override = override

    fake.override = override


def test_order_number_extraction() -> None:
    assert extract_order_number("where is acm-1001 please") == "ACM-1001"
    assert extract_order_number("no number here") is None


@pytest.mark.parametrize(
    "intent,node",
    [
        ("knowledge", "retrieve"),
        ("order", "lookup_order"),
        ("ticket", "create_ticket"),
        ("other", "escalate"),
    ],
)
def test_classified_intent_routes_to_expected_node(intent, node) -> None:
    state = {"tenant_id": "t", "user_id": "u", "messages": [{"role": "user", "content": "anything"}]}
    reply = {"intent": intent, "confidence": 0.9}
    with patch.object(classify_module, "chat_json", return_value=reply):
        update = classify(state)
    assert route_after_classify({**state, **update}) == node


@pytest.mark.parametrize(
    "reply",
    [
        {"intent": "bogus", "confidence": 0.99},
        {"intent": "order", "confidence": "high"},
        {"intent": "order"},
        {},
    ],
)
def test_unusable_or_low_confidence_verdict_routes_to_clarify(reply) -> None:
    state = {"tenant_id": "t", "user_id": "u", "messages": [{"role": "user", "content": "anything"}]}
    with patch.object(classify_module, "chat_json", return_value=reply):
        update = classify(state)
    assert route_after_classify({**state, **update}) == "clarify"


def test_classifier_sees_history_and_finds_order_number_from_earlier_turn() -> None:
    state = {
        "tenant_id": "t",
        "user_id": "u",
        "messages": [
            {"role": "user", "content": "I ordered ACM-1001 last week"},
            {"role": "assistant", "content": "How can I help?"},
            {"role": "user", "content": "where is it?"},
        ],
    }
    with patch.object(classify_module, "chat_json", return_value={"intent": "order", "confidence": 0.9}) as m:
        update = classify(state)
    assert update["order_number"] == "ACM-1001"
    assert "ACM-1001" in m.call_args.args[0][1]["content"]  # history was given to the model


def test_order_lookup_via_chat(api, acme_user) -> None:
    body = ask(api, acme_user, "Where is order ACM-1001?")
    assert body["intent"] == "order"
    assert body["order"]["order_number"] == "ACM-1001"
    assert "shipped" in body["content"]


def test_order_from_other_tenant_not_found_via_chat(api, acme_user) -> None:
    body = ask(api, acme_user, "Where is order GLX-2001?")
    assert body["order"] is None
    assert "couldn't find" in body["content"]


def test_order_without_number_asks_for_it(api, acme_user) -> None:
    body = ask(api, acme_user, "Where is my order?")
    assert body["order"] is None
    assert "order number" in body["content"]


def test_refund_exception_creates_llm_drafted_ticket(api, acme_user, acme_admin, globex_admin) -> None:
    body = ask(api, acme_user, "I need a refund exception")
    assert body["intent"] == "ticket"
    ticket = body["ticket"]
    assert ticket["subject"] == "I need a refund exception"  # drafted by the (fake) LLM
    assert ticket["body"].startswith("Customer reported:")
    assert ticket["id"][:8] in body["content"]
    assert api.get(f"/tickets/{ticket['id']}", headers=acme_user).status_code == 200
    assert api.get(f"/tickets/{ticket['id']}", headers=acme_admin).status_code == 200
    assert api.get(f"/tickets/{ticket['id']}", headers=globex_admin).status_code == 404


def test_faq_still_uses_retrieval(api, acme_user) -> None:
    body = ask(api, acme_user, "What is the return window for returns?")
    assert body["intent"] == "knowledge"
    assert body["citations"] and "30 days" in body["content"]


def test_greeting_escalates_without_side_effects(api, acme_user) -> None:
    body = ask(api, acme_user, "hello")
    assert body["intent"] == "other"
    assert body["ticket"] is None and body["citations"] == []
    assert api.get("/tickets", headers=acme_user).json()["items"] == []


def test_ambiguous_message_gets_clarifying_question_not_a_tool(api, acme_user) -> None:
    body = ask(api, acme_user, "hmm")
    assert "policy" in body["content"] and "ticket" in body["content"]
    assert body["ticket"] is None and body["order"] is None and body["citations"] == []
    assert api.get("/tickets", headers=acme_user).json()["items"] == []


def test_llm_outage_returns_503_and_does_nothing(api, acme_user, fake_llm) -> None:
    def down(path, payload):
        raise LLMUnavailable("boom")

    fake_llm.override = down
    response = api.post("/chat/messages", headers=acme_user, json={"content": "I need a refund exception"})
    assert response.status_code == 503
    assert "temporarily unavailable" in response.json()["detail"]
    fake_llm.override = None
    assert api.get("/tickets", headers=acme_user).json()["items"] == []
    assert api.get("/conversations", headers=acme_user).json() == []  # nothing half-saved


def test_ungrounded_answer_is_refused_without_citations(api, acme_user, fake_llm) -> None:
    def ungrounded(messages):
        if "route messages" in messages[0]["content"]:
            return json.dumps({"intent": "knowledge", "confidence": 0.9})
        return json.dumps({"supported": False, "answer": "Probably 90 days!", "sources": [1]})

    chat_override(fake_llm, ungrounded)
    body = ask(api, acme_user, "What is the return window for returns?")
    assert "couldn't find" in body["content"]
    assert body["citations"] == []
    assert "90" not in body["content"]


def test_only_passages_the_model_used_are_cited(api, acme_user, fake_llm) -> None:
    seen = {}

    def answer(messages):
        if "route messages" in messages[0]["content"]:
            return json.dumps({"intent": "knowledge", "confidence": 0.9})
        seen["prompt"] = messages
        return json.dumps({"supported": True, "answer": "30 days.", "sources": [1, 99, "x"]})

    chat_override(fake_llm, answer)
    body = ask(api, acme_user, "What is the return window for returns?")
    assert len(body["citations"]) == 1  # invalid source numbers ignored
    system = seen["prompt"][0]["content"]
    assert "untrusted" in system and "never follow" in system  # prompt-injection guard


def test_malformed_json_from_model_is_retried_then_503(api, acme_user, fake_llm) -> None:
    chat_override(fake_llm, lambda messages: "this is not json")
    response = api.post("/chat/messages", headers=acme_user, json={"content": "hello"})
    assert response.status_code == 503
    assert len(fake_llm.chat_calls) == 2  # one retry, then give up


def test_principal_mismatch_is_rejected(seeded) -> None:
    """A graph state naming another tenant must not be able to act for the user."""
    from sqlalchemy import select

    from app.agents.graph import get_graph
    from app.db.models import Tenant, User
    from app.db.session import get_session_factory

    with get_session_factory()() as db:
        acme_user = db.scalars(select(User).where(User.email == "acme.user@example.com")).one()
        globex = db.scalars(select(Tenant).where(Tenant.slug == "globex")).one()
        state = {
            "tenant_id": str(globex.id),
            "user_id": str(acme_user.id),
            "messages": [{"role": "user", "content": "Where is order GLX-2001?"}],
        }
        with pytest.raises(PermissionError):
            get_graph().invoke(state, config={"configurable": {"db": db, "user": acme_user}})
