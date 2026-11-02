"""The public /v1 API: authentication, scoping, limits, locking, idempotency, streaming, privacy."""

import json

import pytest
from sqlalchemy import func, select

from app.core import ratelimit
from app.core.llm import LLMUnavailable
from app.db.models import AgentSession, ApiKey, Conversation, Message
from app.db.session import get_session_factory
from tests.test_agents_platform import default_agent, edit_and_publish, new_agent

GOOD = "What is the return window for returns?"


class Bot:
    """A published agent plus a working key, the way a company's backend would hold them."""

    def __init__(self, api, admin_headers, agent_id, key):
        self.api, self.admin, self.id, self.key = api, admin_headers, agent_id, key
        self.auth = {"Authorization": f"Bearer {key}"}

    def send(self, message=GOOD, **extra):
        body = {"message": message, **extra}
        return self.api.post(
            f"/v1/agents/{self.id}/messages", headers=extra.pop("headers", self.auth), json=body
        )

    def configure(self, mutate, note="t"):
        return edit_and_publish(self.api, self.admin, self.id, mutate, note)


def make_bot(api, admin_headers, agent=None) -> Bot:
    agent = agent or default_agent(api, admin_headers)
    key = api.post(f"/admin/agents/{agent['id']}/keys", headers=admin_headers, json={"name": "k"}).json()[
        "key"
    ]
    return Bot(api, admin_headers, agent["id"], key)


@pytest.fixture
def acme_bot(api, acme_admin) -> Bot:
    return make_bot(api, acme_admin)


@pytest.fixture
def globex_bot(api, globex_admin) -> Bot:
    return make_bot(api, globex_admin)


def error(response):
    body = response.json()
    assert set(body) == {"error"}, body
    return body["error"]["code"]


# ---------- authentication & scoping ----------


def test_requires_a_valid_key(api, acme_bot) -> None:
    url = f"/v1/agents/{acme_bot.id}/messages"
    body = {"message": "hi"}
    none = api.post(url, json=body)
    assert none.status_code == 401 and error(none) == "missing_api_key"
    assert none.headers["www-authenticate"] == "Bearer"
    garbage = api.post(url, headers={"Authorization": "Bearer sk_live_nope"}, json=body)
    assert garbage.status_code == 401 and error(garbage) == "invalid_api_key"
    assert api.post(url, headers={"Authorization": "Basic abc"}, json=body).status_code == 401
    # the dashboard's JWT is not an API key
    assert (
        api.post(
            url, headers={"Authorization": f"Bearer {acme_bot.admin['Authorization'][7:]}"}, json=body
        ).status_code
        == 401
    )


def test_key_only_works_for_its_own_agent(api, acme_bot, acme_admin, globex_bot) -> None:
    other = new_agent(api, acme_admin, "Other agent")
    edit_and_publish(api, acme_admin, other["id"], lambda c: None)
    r = api.post(f"/v1/agents/{other['id']}/messages", headers=acme_bot.auth, json={"message": "hi"})
    assert r.status_code == 403 and error(r) == "agent_mismatch"
    # another company's key against our agent, and ours against theirs
    cross = api.post(f"/v1/agents/{acme_bot.id}/messages", headers=globex_bot.auth, json={"message": "hi"})
    assert cross.status_code == 403
    cross2 = api.post(f"/v1/agents/{globex_bot.id}/messages", headers=acme_bot.auth, json={"message": "hi"})
    assert cross2.status_code == 403
    unknown = api.post(
        "/v1/agents/00000000-0000-0000-0000-000000000000/messages",
        headers=acme_bot.auth,
        json={"message": "hi"},
    )
    assert unknown.status_code == 403 and error(unknown) == "agent_mismatch"  # same answer: no probing


def test_last_used_is_recorded(api, acme_bot) -> None:
    acme_bot.send()
    with get_session_factory()() as db:
        assert db.scalars(select(ApiKey)).one().last_used_at is not None


def test_unpublished_agent_returns_conflict(api, acme_admin) -> None:
    agent = new_agent(api, acme_admin, "Draft only")
    bot = make_bot(api, acme_admin, agent)
    r = bot.send()
    assert r.status_code == 409 and error(r) == "agent_not_published"


def test_validation_errors_use_the_public_error_shape(api, acme_bot) -> None:
    for body in (
        {"message": ""},
        {},
        {"message": "x", "session_id": "bad id!"},
        {"message": "x", "end_user": {"id": ""}},
    ):
        r = api.post(f"/v1/agents/{acme_bot.id}/messages", headers=acme_bot.auth, json=body)
        assert r.status_code == 422 and error(r) == "invalid_request", body


# ---------- answering ----------


def test_answers_with_citations_and_a_generated_session(api, acme_bot) -> None:
    r = acme_bot.send()
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "completed" and body["outcome"] == "answered"
    assert "30 days" in body["reply"]
    assert body["citations"][0]["title"] == "Acme Return Policy"
    assert body["session_id"].startswith("s_")
    assert body["usage"]["tokens"] > 0 and body["actions_taken"] == [] and body["handoff"] is None
    assert body["pending_confirmation"] is None and body["action_request"] is None


def test_unanswerable_question_is_reported_as_refused(api, acme_bot) -> None:
    body = acme_bot.send("zzzqx plugh xyzzy").json()
    assert body["outcome"] == "refused" and body["citations"] == []
    assert "couldn't find" in body["reply"]


def test_session_continuity_and_end_user_are_stored(api, acme_bot) -> None:
    end_user = {"id": "cust_8142", "attributes": {"plan": "pro"}}
    first = acme_bot.send(session_id="web-1", end_user=end_user).json()
    acme_bot.send("and shipping?", session_id="web-1", end_user=end_user)
    assert first["session_id"] == "web-1"
    with get_session_factory()() as db:
        session = db.scalars(select(AgentSession)).one()
        assert (session.external_id, session.end_user_ref, session.end_user_attrs) == (
            "web-1",
            "cust_8142",
            {"plan": "pro"},
        )
        conversation = db.scalars(select(Conversation)).one()
        assert conversation.source == "api" and conversation.user_id is None
        roles = [m.role.value for m in db.scalars(select(Message).order_by(Message.created_at))]
        assert roles == ["user", "assistant", "user", "assistant"]


def test_session_cannot_be_shared_between_end_users(api, acme_bot) -> None:
    acme_bot.send(session_id="shared", end_user={"id": "alice"})
    r = acme_bot.send(session_id="shared", end_user={"id": "bob"})
    assert r.status_code == 409 and error(r) == "session_user_mismatch"


def test_instructions_apply_only_after_publishing(api, acme_bot, fake_llm) -> None:
    def system_prompts():
        return [
            call[0]["content"]
            for call in fake_llm.chat_calls
            if "numbered context passages" in call[0]["content"]
        ]

    acme_bot.configure(lambda c: c.update(instructions="Always answer like a pirate."))
    acme_bot.send()
    assert "Always answer like a pirate." in system_prompts()[-1]

    config = api.get(f"/admin/agents/{acme_bot.id}", headers=acme_bot.admin).json()["draft_config"]
    config["instructions"] = "Speak in rhymes."
    api.put(f"/admin/agents/{acme_bot.id}/draft", headers=acme_bot.admin, json=config)
    acme_bot.send("What about shipping speeds?")
    assert "rhymes" not in system_prompts()[-1]  # the draft is not live


def test_not_found_message_is_configurable(api, acme_bot) -> None:
    acme_bot.configure(lambda c: c["knowledge"].update(not_found_message="Please email help@acme.test."))
    assert acme_bot.send("zzzqx plugh xyzzy").json()["reply"] == "Please email help@acme.test."


def test_agents_only_know_their_attached_collections(api, acme_admin, acme_bot) -> None:
    cid = api.post("/admin/collections", headers=acme_admin, json={"name": "Secret"}).json()["id"]
    api.post(
        "/admin/documents", headers=acme_admin,
        json={"title": "Gadget", "content": "The zorblax gadget ships in blue.", "collection_id": cid},
    )  # fmt: skip
    question = "What colour does the zorblax gadget ship in?"

    def sees_gadget(reply: dict) -> bool:
        return "blue" in reply["reply"] or any(c["title"] == "Gadget" for c in reply["citations"])

    assert not sees_gadget(acme_bot.send(question).json())  # collection not attached yet

    second = new_agent(api, acme_admin, "Gadget bot", template="blank")
    gadget_bot = make_bot(api, acme_admin, second)
    gadget_bot.configure(lambda c: c["knowledge"].update(collection_ids=[cid]))
    answer = gadget_bot.send(question).json()
    assert answer["outcome"] == "answered" and sees_gadget(answer)
    # ...and the first agent still can't see it, even though the retrieval cache is warm
    assert not sees_gadget(acme_bot.send(question).json())


def test_agent_with_no_collections_answers_nothing(api, acme_admin) -> None:
    blank = new_agent(api, acme_admin, "Empty", template="blank")
    bot = make_bot(api, acme_admin, blank)
    bot.configure(lambda c: None)
    assert bot.send(GOOD).json()["outcome"] == "refused"


def test_tenants_stay_isolated_through_the_public_api(api, acme_bot, globex_bot) -> None:
    acme = acme_bot.send("What is the return window for returns?").json()
    globex = globex_bot.send("What is the return window for returns?").json()
    assert "30 days" in acme["reply"] and "60 days" in globex["reply"]
    leak = acme_bot.send("Tell me about the Globex Flex Credit program").json()
    assert all("Globex" not in c["title"] for c in leak["citations"])
    # the same session id in two workspaces are unrelated sessions
    acme_bot.send(session_id="same", end_user={"id": "u1"})
    globex_bot.send(session_id="same", end_user={"id": "u2"})


def test_model_outage_returns_503_and_keeps_the_error_shape(api, acme_bot, fake_llm) -> None:
    def down(path, payload):
        raise LLMUnavailable("down")

    fake_llm.override = down
    r = acme_bot.send()
    assert r.status_code == 503 and error(r) == "model_unavailable" and r.headers["retry-after"]
    fake_llm.override = None
    assert acme_bot.send().status_code == 200
    # the failed attempt must not have left the concurrency slot or session lock held
    assert acme_bot.send(session_id="after").status_code == 200


# ---------- limits ----------


def test_rate_limit_headers_and_per_key_limit(api, acme_bot) -> None:
    acme_bot.configure(lambda c: c["limits"].update(requests_per_minute_per_key=3))
    first = acme_bot.send()
    assert first.headers["x-ratelimit-limit"] == "3" and first.headers["x-ratelimit-remaining"] == "2"
    acme_bot.send()
    acme_bot.send()
    blocked = acme_bot.send()
    assert blocked.status_code == 429 and error(blocked) == "rate_limited"
    assert 1 <= int(blocked.headers["retry-after"]) <= 60 and blocked.headers["x-ratelimit-remaining"] == "0"


def test_per_end_user_limit_does_not_affect_other_users(api, acme_bot) -> None:
    acme_bot.configure(lambda c: c["limits"].update(requests_per_minute_per_end_user=2))
    alice = {"id": "alice"}
    assert acme_bot.send(end_user=alice).status_code == 200
    assert acme_bot.send(end_user=alice).status_code == 200
    assert acme_bot.send(end_user=alice).status_code == 429
    assert acme_bot.send(end_user={"id": "bob"}).status_code == 200


def test_workspace_wide_limit(api, acme_bot, monkeypatch) -> None:
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "api_rate_per_tenant", 2)
    assert [acme_bot.send().status_code for _ in range(3)] == [200, 200, 429]


def test_concurrency_cap(api, acme_bot) -> None:
    acme_bot.configure(lambda c: c["limits"].update(max_concurrent_requests=1))
    with get_session_factory()() as db:
        key_id = str(db.scalars(select(ApiKey)).one().id)
    assert ratelimit.acquire_slot(key_id, limit=1)  # another request is "running"
    try:
        r = acme_bot.send()
        assert r.status_code == 429 and error(r) == "too_many_concurrent_requests"
    finally:
        ratelimit.release_slot(key_id)
    assert acme_bot.send().status_code == 200  # and the rejected attempt did not leak a slot


def test_one_message_at_a_time_per_session(api, acme_bot) -> None:
    lock = ratelimit.acquire_lock(f"{acme_bot.id}:busy-session")
    assert lock not in (None, True)
    try:
        r = acme_bot.send(session_id="busy-session")
        assert r.status_code == 409 and error(r) == "session_busy"
        assert acme_bot.send(session_id="other-session").status_code == 200
    finally:
        ratelimit.release_lock(lock)
    assert acme_bot.send(session_id="busy-session").status_code == 200


# ---------- idempotency ----------


def test_idempotent_retry_returns_the_stored_response(api, acme_bot, fake_llm) -> None:
    headers = {**acme_bot.auth, "Idempotency-Key": "order-1"}
    body = {"message": GOOD, "session_id": "idem"}
    url = f"/v1/agents/{acme_bot.id}/messages"
    first = api.post(url, headers=headers, json=body)
    calls_after_first = len(fake_llm.chat_calls)
    again = api.post(url, headers=headers, json=body)
    assert again.status_code == 200 and again.headers["idempotent-replay"] == "true"
    assert again.json()["message_id"] == first.json()["message_id"]
    assert len(fake_llm.chat_calls) == calls_after_first  # nothing ran again
    with get_session_factory()() as db:
        assert db.scalar(select(func.count()).select_from(Message)) == 2  # one user + one assistant


def test_idempotency_key_reuse_with_a_different_body_is_rejected(api, acme_bot) -> None:
    headers = {**acme_bot.auth, "Idempotency-Key": "k-1"}
    url = f"/v1/agents/{acme_bot.id}/messages"
    api.post(url, headers=headers, json={"message": GOOD})
    r = api.post(url, headers=headers, json={"message": "something else"})
    assert r.status_code == 422 and error(r) == "idempotency_key_reuse"


def test_failed_attempt_can_be_retried_with_the_same_key(api, acme_bot, fake_llm) -> None:
    headers = {**acme_bot.auth, "Idempotency-Key": "k-2"}
    url = f"/v1/agents/{acme_bot.id}/messages"
    fake_llm.override = lambda path, payload: (_ for _ in ()).throw(LLMUnavailable("down"))
    assert api.post(url, headers=headers, json={"message": GOOD}).status_code == 503
    fake_llm.override = None
    retry = api.post(url, headers=headers, json={"message": GOOD})
    assert retry.status_code == 200 and "idempotent-replay" not in retry.headers


def test_idempotency_is_scoped_to_the_key_owner(api, acme_bot, globex_bot) -> None:
    a = api.post(
        f"/v1/agents/{acme_bot.id}/messages",
        headers={**acme_bot.auth, "Idempotency-Key": "same"},
        json={"message": GOOD},
    ).json()
    g = api.post(
        f"/v1/agents/{globex_bot.id}/messages",
        headers={**globex_bot.auth, "Idempotency-Key": "same"},
        json={"message": GOOD},
    ).json()
    assert "30 days" in a["reply"] and "60 days" in g["reply"]


def test_idempotency_key_with_streaming_is_refused(api, acme_bot) -> None:
    r = api.post(
        f"/v1/agents/{acme_bot.id}/messages",
        headers={**acme_bot.auth, "Idempotency-Key": "s"},
        json={"message": GOOD, "stream": True},
    )
    assert r.status_code == 422


# ---------- streaming ----------


def sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.strip().split("\n\n"):
        name, data = block.split("\n")
        events.append((name.removeprefix("event: "), json.loads(data.removeprefix("data: "))))
    return events


def test_stream_emits_stages_then_done(api, acme_bot) -> None:
    r = api.post(
        f"/v1/agents/{acme_bot.id}/messages", headers=acme_bot.auth,
        json={"message": GOOD, "stream": True, "session_id": "st-1"},
    )  # fmt: skip
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = sse(r.text)
    assert [n for n, _ in events] == ["stage", "stage", "done"]
    assert [d["node"] for n, d in events if n == "stage"] == ["retrieve", "generate"]
    done = events[-1][1]
    assert done["session_id"] == "st-1" and "30 days" in done["reply"] and done["usage"]["tokens"] > 0
    assert r.headers["x-ratelimit-limit"] == "60"
    # lock and slot are released once the stream ends
    assert acme_bot.send(session_id="st-1").status_code == 200


def test_stream_reports_model_outage_as_an_event_and_releases_resources(api, acme_bot, fake_llm) -> None:
    fake_llm.override = lambda path, payload: (_ for _ in ()).throw(LLMUnavailable("down"))
    r = api.post(
        f"/v1/agents/{acme_bot.id}/messages", headers=acme_bot.auth,
        json={"message": GOOD, "stream": True, "session_id": "st-2"},
    )  # fmt: skip
    events = sse(r.text)
    assert events[-1][0] == "error" and events[-1][1]["code"] == "model_unavailable"
    fake_llm.override = None
    assert acme_bot.send(session_id="st-2").status_code == 200


def test_stream_is_rejected_before_starting_when_unauthorised_or_limited(api, acme_bot) -> None:
    url = f"/v1/agents/{acme_bot.id}/messages"
    assert api.post(url, json={"message": GOOD, "stream": True}).status_code == 401
    acme_bot.configure(lambda c: c["limits"].update(requests_per_minute_per_key=1))
    api.post(url, headers=acme_bot.auth, json={"message": GOOD, "stream": True})
    second = api.post(url, headers=acme_bot.auth, json={"message": GOOD, "stream": True})
    assert second.status_code == 429  # a real status, not an in-stream error


# ---------- sessions & privacy ----------


def test_session_state_and_deletion(api, acme_bot, globex_bot) -> None:
    acme_bot.send(session_id="del-1", end_user={"id": "u1"})
    state = api.get(f"/v1/agents/{acme_bot.id}/sessions/del-1", headers=acme_bot.auth).json()
    assert state["end_user_id"] == "u1" and state["message_count"] == 2 and state["active_run"] is None
    # another workspace cannot see or delete it
    assert api.get(f"/v1/agents/{globex_bot.id}/sessions/del-1", headers=globex_bot.auth).status_code == 404
    assert (
        api.delete(f"/v1/agents/{globex_bot.id}/sessions/del-1", headers=globex_bot.auth).status_code == 404
    )

    assert api.delete(f"/v1/agents/{acme_bot.id}/sessions/del-1", headers=acme_bot.auth).status_code == 204
    assert api.get(f"/v1/agents/{acme_bot.id}/sessions/del-1", headers=acme_bot.auth).status_code == 404
    assert api.delete(f"/v1/agents/{acme_bot.id}/sessions/del-1", headers=acme_bot.auth).status_code == 404
    with get_session_factory()() as db:
        assert db.scalar(select(func.count()).select_from(Message)) == 0  # transcript is gone too


def test_delete_all_data_of_one_end_user(api, acme_bot) -> None:
    for sid in ("a", "b"):
        acme_bot.send(session_id=sid, end_user={"id": "carol"})
    acme_bot.send(session_id="c", end_user={"id": "dave"})
    r = api.delete(f"/v1/agents/{acme_bot.id}/end-users/carol", headers=acme_bot.auth)
    assert r.json() == {"deleted_sessions": 2}
    assert api.get(f"/v1/agents/{acme_bot.id}/sessions/c", headers=acme_bot.auth).status_code == 200


def test_transcript_storage_can_be_switched_off(api, acme_bot) -> None:
    acme_bot.configure(lambda c: c["privacy"].update(store_transcript="none"))
    r = acme_bot.send(session_id="private")
    assert r.status_code == 200 and "30 days" in r.json()["reply"] and r.json()["message_id"] is None
    with get_session_factory()() as db:
        assert db.scalar(select(func.count()).select_from(Message)) == 0
        assert db.scalar(select(func.count()).select_from(AgentSession)) == 1  # the session record remains


# ---------- dashboard playground ----------


def test_playground_runs_the_draft_unlike_the_public_api(api, acme_admin, acme_bot, fake_llm) -> None:
    def used_prompt():
        return [
            c[0]["content"] for c in fake_llm.chat_calls if "numbered context passages" in c[0]["content"]
        ][-1]

    config = api.get(f"/admin/agents/{acme_bot.id}", headers=acme_admin).json()["draft_config"]
    config["instructions"] = "DRAFT-ONLY-GUIDANCE"
    api.put(f"/admin/agents/{acme_bot.id}/draft", headers=acme_admin, json=config)

    url = f"/admin/agents/{acme_bot.id}/playground/messages"
    draft = api.post(url, headers=acme_admin, json={"message": GOOD})
    assert draft.status_code == 200 and "DRAFT-ONLY-GUIDANCE" in used_prompt()
    live = api.post(url, headers=acme_admin, json={"message": GOOD, "version": "published"})
    assert live.status_code == 200 and "DRAFT-ONLY-GUIDANCE" not in used_prompt()
    acme_bot.send()
    assert "DRAFT-ONLY-GUIDANCE" not in used_prompt()


def test_playground_is_staff_only_and_keeps_sessions(api, acme_user, acme_admin, acme_bot) -> None:
    url = f"/admin/agents/{acme_bot.id}/playground/messages"
    assert api.post(url, headers=acme_user, json={"message": "hi"}).status_code == 403
    first = api.post(url, headers=acme_admin, json={"message": GOOD}).json()
    second = api.post(
        url, headers=acme_admin, json={"message": "and shipping?", "session_id": first["session_id"]}
    )
    assert second.status_code == 200 and first["session_id"].startswith("pg_")
    never_published = new_agent(api, acme_admin, "Unpublished")
    r = api.post(
        f"/admin/agents/{never_published['id']}/playground/messages", headers=acme_admin,
        json={"message": "hi", "version": "published"},
    )  # fmt: skip
    assert r.status_code == 409


def test_default_agent_matches_the_old_assistant_for_existing_data(api, acme_admin) -> None:
    """The seeded default agent is published and covers the tenant's existing documents."""
    agent = default_agent(api, acme_admin)
    key = api.post(f"/admin/agents/{agent['id']}/keys", headers=acme_admin, json={"name": "k"}).json()["key"]
    r = api.post(
        f"/v1/agents/{agent['id']}/messages", headers={"Authorization": f"Bearer {key}"},
        json={"message": "How fast is express shipping?"},
    )  # fmt: skip
    assert r.status_code == 200 and r.json()["outcome"] == "answered"
