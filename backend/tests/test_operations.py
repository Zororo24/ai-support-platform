"""Phase N: daily budgets and alerts, API key address limits, retention, redaction, and a
workspace's own AI provider key."""

import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import select, update

from app.core import llm, pii
from app.core.config import get_settings
from app.core.llm import LLMUnavailable
from app.db.models import AgentSession, Conversation, Message, MessageRole, Run, RunStep, Tenant
from app.db.session import get_session_factory
from app.services import agents as agents_service
from app.services import housekeeping, tenant_llm
from tests.test_jobs_and_webhooks import make_hook
from tests.test_llm_client import REAL_POST, response
from tests.test_public_api import error, make_bot

USER = {"id": "customer-1"}


def set_today_tokens(total: int) -> None:
    """Make today's usage exactly `total` tokens (all of it on the most recent run)."""
    with get_session_factory()() as db:
        db.execute(update(Run).values(tokens=0))
        latest = db.scalars(select(Run).order_by(Run.started_at.desc())).first()
        latest.tokens = total
        db.commit()


def message(api, agent_id, key, text="hello", **headers):
    return api.post(
        f"/v1/agents/{agent_id}/messages",
        headers={"Authorization": f"Bearer {key}", **headers},
        json={"message": text, "end_user": USER},
    )


# ---------- daily budgets ----------


def test_an_agent_that_used_its_daily_budget_is_paused_until_midnight(api, acme_admin, globex_admin) -> None:
    bot = make_bot(api, acme_admin)
    bot.configure(lambda c: c["limits"].update(daily_token_budget=1000))
    other = make_bot(api, globex_admin)
    assert bot.send("hello", end_user=USER).status_code == 200
    set_today_tokens(1500)

    blocked = bot.send("hello again", end_user=USER)
    assert blocked.status_code == 429 and error(blocked) == "budget_exceeded"
    assert "This agent has used its daily AI budget" in blocked.json()["error"]["message"]
    assert 1 <= int(blocked.headers["Retry-After"]) <= 24 * 3600
    assert other.send("hello", end_user=USER).status_code == 200  # another workspace is not affected

    with get_session_factory()() as db:  # tomorrow's budget starts fresh
        db.execute(update(Run).values(started_at=datetime.now(timezone.utc) - timedelta(days=1, hours=1)))
        db.commit()
    assert bot.send("hello again", end_user=USER).status_code == 200


def test_budgets_warn_at_80_percent_and_again_when_used_up_once_per_day(api, acme_admin, receiver) -> None:
    bot = make_bot(api, acme_admin)
    bot.configure(lambda c: c["limits"].update(daily_token_budget=1000))
    make_hook(api, acme_admin, events=["budget.warning", "budget.exceeded"])
    bot.send("hello", end_user=USER)
    assert receiver.requests == []  # nowhere near the limit

    set_today_tokens(850)
    bot.send("hello again", end_user=USER)
    (warning,) = receiver.events("budget.warning")
    assert warning["data"]["daily_budget"] == 1000 and warning["data"]["used_tokens"] >= 850
    assert receiver.events("budget.exceeded") == []

    set_today_tokens(999)  # the next turn pushes it over
    bot.send("one more", end_user=USER)
    assert len(receiver.events("budget.exceeded")) == 1
    assert len(receiver.events("budget.warning")) == 1  # not repeated

    assert message(api, bot.id, bot.key).status_code == 429
    assert len(receiver.events("budget.exceeded")) == 1


def test_a_workspace_ceiling_covers_all_of_its_agents(api, acme_admin, globex_admin, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "tenant_daily_token_budget", 1000)
    first = make_bot(api, acme_admin)
    globex = make_bot(api, globex_admin)
    assert first.send("hello", end_user=USER).status_code == 200
    with get_session_factory()() as db:  # push only Acme over its ceiling
        run = db.scalars(select(Run)).first()
        run.tokens = 5000
        db.commit()
    blocked = first.send("hello", end_user=USER)
    assert blocked.status_code == 429 and "This workspace has used" in blocked.json()["error"]["message"]
    assert globex.send("hello", end_user=USER).status_code == 200


def test_usage_shows_todays_tokens_against_each_budget(api, acme_admin) -> None:
    bot = make_bot(api, acme_admin)
    bot.configure(lambda c: c["limits"].update(daily_token_budget=5000))
    bot.send("hello", end_user=USER)
    rows = api.get("/admin/usage?days=7", headers=acme_admin).json()["by_agent"]
    row = next(r for r in rows if r["agent_id"] == bot.id)
    assert row["daily_budget"] == 5000 and row["tokens_today"] > 0


def test_playground_testing_is_never_blocked_by_the_budget(api, acme_admin) -> None:
    bot = make_bot(api, acme_admin)
    bot.configure(lambda c: c["limits"].update(daily_token_budget=1000))
    bot.send("hello", end_user=USER)
    set_today_tokens(9999)
    tried = api.post(
        f"/admin/agents/{bot.id}/playground/messages", headers=acme_admin, json={"message": "hi"}
    )
    assert tried.status_code == 200


# ---------- API key address limits ----------


def restricted_key(api, admin, agent_id, ips):
    created = api.post(
        f"/admin/agents/{agent_id}/keys", headers=admin, json={"name": "restricted", "allowed_ips": ips}
    )
    assert created.status_code == 201, created.text
    return created.json()


def test_a_key_limited_to_addresses_refuses_everyone_else(api, acme_admin, monkeypatch) -> None:
    bot = make_bot(api, acme_admin)
    key = restricted_key(api, acme_admin, bot.id, ["203.0.113.0/24", "198.51.100.7"])["key"]
    assert key and restricted_key(api, acme_admin, bot.id, [])["allowed_ips"] == []

    refused = message(api, bot.id, key)  # the test client's address is not an IP at all
    assert refused.status_code == 403 and error(refused) == "ip_not_allowed"

    # Without a trusted proxy, a forwarded header is just something the caller wrote.
    assert message(api, bot.id, key, **{"X-Forwarded-For": "203.0.113.9"}).status_code == 403

    monkeypatch.setattr(get_settings(), "trust_proxy_headers", True)
    assert message(api, bot.id, key, **{"X-Forwarded-For": "203.0.113.9"}).status_code == 200
    assert message(api, bot.id, key, **{"X-Forwarded-For": "198.51.100.7"}).status_code == 200
    assert message(api, bot.id, key, **{"X-Forwarded-For": "198.51.100.8"}).status_code == 403
    # Only the address the proxy itself appended counts; an earlier entry could be forged.
    assert message(api, bot.id, key, **{"X-Forwarded-For": "203.0.113.9, 192.0.2.1"}).status_code == 403
    assert message(api, bot.id, key, **{"X-Forwarded-For": "192.0.2.1, 203.0.113.9"}).status_code == 200


def test_a_wrong_key_is_still_unauthorised_not_forbidden(api, acme_admin, monkeypatch) -> None:
    bot = make_bot(api, acme_admin)
    restricted_key(api, acme_admin, bot.id, ["203.0.113.7"])
    wrong = message(api, bot.id, "sk_live_notakey")
    assert wrong.status_code == 401  # nothing is revealed about which keys are restricted


def test_address_limits_can_be_changed_and_removed(api, acme_admin, globex_admin, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "trust_proxy_headers", True)
    bot = make_bot(api, acme_admin)
    created = restricted_key(api, acme_admin, bot.id, ["203.0.113.7"])
    url = f"/admin/agents/{bot.id}/keys/{created['id']}/allowed-ips"
    assert message(api, bot.id, created["key"], **{"X-Forwarded-For": "192.0.2.1"}).status_code == 403

    assert api.put(url, headers=globex_admin, json={"allowed_ips": []}).status_code == 404
    changed = api.put(url, headers=acme_admin, json={"allowed_ips": ["192.0.2.0/24", "2001:db8::/32"]})
    assert changed.status_code == 200 and changed.json()["allowed_ips"] == ["192.0.2.0/24", "2001:db8::/32"]
    assert message(api, bot.id, created["key"], **{"X-Forwarded-For": "192.0.2.1"}).status_code == 200
    assert message(api, bot.id, created["key"], **{"X-Forwarded-For": "2001:db8::5"}).status_code == 200

    cleared = api.put(url, headers=acme_admin, json={"allowed_ips": []})
    assert cleared.json()["allowed_ips"] == []
    assert message(api, bot.id, created["key"], **{"X-Forwarded-For": "8.8.8.8"}).status_code == 200
    listed = api.get(f"/admin/agents/{bot.id}/keys", headers=acme_admin).json()
    assert next(k for k in listed if k["id"] == created["id"])["allowed_ips"] == []


def test_bad_addresses_are_rejected_when_saved(api, acme_admin) -> None:
    bot = make_bot(api, acme_admin)
    for bad in (["not-an-ip"], ["10.0.0.0/33"], ["203.0.113.7", "banana"]):
        response_ = api.post(
            f"/admin/agents/{bot.id}/keys", headers=acme_admin, json={"name": "k", "allowed_ips": bad}
        )
        assert response_.status_code == 422, bad
    too_many = [f"10.0.{i}.1" for i in range(21)]
    assert (
        api.post(
            f"/admin/agents/{bot.id}/keys", headers=acme_admin, json={"name": "k", "allowed_ips": too_many}
        ).status_code
        == 422
    )


def test_address_matching_rules() -> None:
    class Key:
        allowed_ips = ["203.0.113.0/24", "198.51.100.7/32", "2001:db8::/32"]

    assert agents_service.parse_allowed_ips([" 10.0.0.5 ", "10.0.0.5", "", "10.0.0.0/8"]) == [
        "10.0.0.5/32",
        "10.0.0.0/8",
    ]
    assert agents_service.ip_allowed(Key, "203.0.113.200") and agents_service.ip_allowed(Key, "198.51.100.7")
    assert agents_service.ip_allowed(Key, "2001:db8:1::1")
    assert not agents_service.ip_allowed(Key, "198.51.100.8") and not agents_service.ip_allowed(
        Key, "2002::1"
    )
    assert not agents_service.ip_allowed(Key, None) and not agents_service.ip_allowed(Key, "testclient")

    class Open:
        allowed_ips: list[str] = []

    assert agents_service.ip_allowed(Open, None) and agents_service.ip_allowed(Open, "anything")


# ---------- retention ----------


def test_old_conversations_are_deleted_per_the_agents_retention_setting(
    api, acme_admin, globex_admin
) -> None:
    bot = make_bot(api, acme_admin)
    bot.configure(lambda c: c["privacy"].update(retention_days=1))
    other = make_bot(api, globex_admin)  # keeps the default 30 days
    for session in ("old", "recent"):
        bot.send("hello", session_id=session, end_user=USER)
    other.send("hello", session_id="old", end_user=USER)
    long_ago = datetime.now(timezone.utc) - timedelta(days=3)
    with get_session_factory()() as db:
        for row in db.scalars(select(AgentSession).where(AgentSession.external_id == "old")):
            row.last_active_at = long_ago
        db.commit()

    with get_session_factory()() as db:
        assert housekeeping.run_sweep(db)["sessions_expired"] == 1

    assert api.get(f"/v1/agents/{bot.id}/sessions/old", headers=bot.auth).status_code == 404
    assert api.get(f"/v1/agents/{bot.id}/sessions/recent", headers=bot.auth).status_code == 200
    assert (
        api.get(f"/v1/agents/{other.id}/sessions/old", headers=other.auth).status_code == 200
    )  # 30-day agent
    with get_session_factory()() as db:
        assert db.scalars(select(AgentSession).where(AgentSession.agent_id == uuid.UUID(bot.id))).all()
        assert (
            len(db.scalars(select(Conversation).where(Conversation.agent_id == uuid.UUID(bot.id))).all()) == 1
        )


# ---------- redaction ----------


def test_masking_removes_personal_data_but_not_ordinary_numbers() -> None:
    masked = pii.mask_text(
        "Mail jo.smith@example.com, card 4111 1111 1111 1111 or 4111-1111-1111-1111, "
        "call +44 20 7946 0958 or (415) 555-0132 or 0412 345 678."
    )
    assert "[email]" in masked and masked.count("[card]") == 2 and masked.count("[phone]") == 3
    assert (
        "example.com" not in masked
        and "4111" not in masked
        and "7946" not in masked
        and "555-0132" not in masked
    )
    for harmless in (
        "Order ACM-1002 shipped on 2026-11-02 for $149.99",
        "My account number is 12345678",
        "Your card 1234 5678 9012 3456 is invalid",  # not a valid card number (fails the Luhn check)
        "Item SKU-2000-0001",
    ):
        assert pii.mask_text(harmless) == harmless
    assert pii.mask_text(masked) == masked  # masking twice changes nothing


def test_masking_walks_nested_data() -> None:
    assert pii.mask_deep({"a": ["x@y.io", {"b": "ok", "n": 3}], "c": None}) == {
        "a": ["[email]", {"b": "ok", "n": 3}],
        "c": None,
    }


def test_stored_conversations_are_masked_but_the_model_sees_the_real_text(api, acme_admin, fake_llm) -> None:
    bot = make_bot(api, acme_admin)
    bot.configure(lambda c: c["privacy"].update(redact_pii=True))
    secret = "My email is jo@example.com and my card is 4111 1111 1111 1111"
    reply = bot.send(secret, end_user=USER).json()
    assert "jo@example.com" in json.dumps(fake_llm.chat_calls[-1])  # the model needed it to help

    with get_session_factory()() as db:
        stored = [m.content for m in db.scalars(select(Message).where(Message.role == MessageRole.user))]
        assert stored == ["My email is [email] and my card is [card]"]
        run = db.get(Run, uuid.UUID(reply["run_id"]))
        assert "jo@example.com" not in (run.input_preview or "") and "[email]" in run.input_preview
        steps = db.scalars(select(RunStep).where(RunStep.run_id == run.id)).all()
        assert steps and "jo@example.com" not in json.dumps([s.input for s in steps])
        assert "4111" not in json.dumps([s.input for s in steps])


def test_without_the_setting_nothing_is_masked(api, acme_admin) -> None:
    bot = make_bot(api, acme_admin)
    bot.send("My email is jo@example.com", end_user=USER)
    with get_session_factory()() as db:
        assert [m.content for m in db.scalars(select(Message).where(Message.role == MessageRole.user))] == [
            "My email is jo@example.com"
        ]


# ---------- the workspace's own provider key ----------


def spy_on_keys(fake):
    seen: list[tuple[str, str | None]] = []

    def spy(path, payload):
        seen.append((path, (llm._usage.get() or {}).get("api_key")))
        fake.override = None
        try:
            return fake(path, payload)
        finally:
            fake.override = spy

    fake.override = spy
    return seen


def test_a_workspace_can_bring_its_own_key_and_only_it_uses_it(
    api, acme_admin, globex_admin, fake_llm
) -> None:
    seen = spy_on_keys(fake_llm)
    saved = api.put("/admin/settings/llm-key", headers=acme_admin, json={"api_key": "sk-acme-own-key-1234"})
    assert saved.status_code == 200, saved.text
    assert saved.json() == {"configured": True, "hint": saved.json()["hint"]} and saved.json()[
        "hint"
    ].endswith("1234")
    assert "sk-acme-own-key" not in saved.text
    assert seen[-1] == ("/embeddings", "sk-acme-own-key-1234")  # checked with the provider using that key

    acme, globex = make_bot(api, acme_admin), make_bot(api, globex_admin)
    seen.clear()
    acme.send("what is the warranty?", end_user=USER)
    assert seen and {key for _, key in seen} == {"sk-acme-own-key-1234"}
    seen.clear()
    globex.send("what is the warranty?", end_user=USER)
    assert seen and {key for _, key in seen} == {None}  # the platform's key

    with get_session_factory()() as db:
        stored = db.scalars(select(Tenant).where(Tenant.slug == "acme")).one().llm_api_key_encrypted
        assert stored and "sk-acme-own-key" not in stored  # encrypted at rest
    assert api.get("/admin/settings/llm-key", headers=acme_admin).json()["configured"] is True
    assert api.get("/admin/settings/llm-key", headers=globex_admin).json() == {
        "configured": False,
        "hint": None,
    }


def test_the_workspace_key_covers_indexing_and_dashboard_testing_too(api, acme_admin, fake_llm) -> None:
    api.put("/admin/settings/llm-key", headers=acme_admin, json={"api_key": "sk-acme-own-key-1234"})
    seen = spy_on_keys(fake_llm)
    api.post(
        "/admin/documents",
        headers=acme_admin,
        json={"title": "Warranty", "content": "Two years from purchase."},
    )
    assert ("/embeddings", "sk-acme-own-key-1234") in seen
    seen.clear()
    api.post("/admin/retrieval/test", headers=acme_admin, json={"query": "warranty"})
    assert seen and {key for _, key in seen} == {"sk-acme-own-key-1234"}


def test_a_key_the_provider_rejects_is_not_saved(api, acme_admin, fake_llm) -> None:
    fake_llm.override = lambda path, payload: (_ for _ in ()).throw(LLMUnavailable("401"))
    rejected = api.put("/admin/settings/llm-key", headers=acme_admin, json={"api_key": "sk-not-valid-at-all"})
    assert rejected.status_code == 422 and "did not accept" in rejected.json()["detail"]
    fake_llm.override = None
    assert api.get("/admin/settings/llm-key", headers=acme_admin).json()["configured"] is False
    assert (
        api.put("/admin/settings/llm-key", headers=acme_admin, json={"api_key": "short"}).status_code == 422
    )


def test_removing_the_key_returns_to_the_platform_key(api, acme_admin, fake_llm) -> None:
    api.put("/admin/settings/llm-key", headers=acme_admin, json={"api_key": "sk-acme-own-key-1234"})
    cleared = api.delete("/admin/settings/llm-key", headers=acme_admin)
    assert cleared.json() == {"configured": False, "hint": None}
    seen = spy_on_keys(fake_llm)
    make_bot(api, acme_admin).send("what is the warranty?", end_user=USER)
    assert seen and {key for _, key in seen} == {None}


def test_only_admins_manage_the_key(api, acme_user) -> None:
    assert api.get("/admin/settings/llm-key", headers=acme_user).status_code == 403
    assert (
        api.put(
            "/admin/settings/llm-key", headers=acme_user, json={"api_key": "sk-acme-own-key-1234"}
        ).status_code
        == 403
    )


def test_an_unreadable_workspace_key_fails_loudly_instead_of_using_the_platform_key(
    api, acme_admin, fake_llm
) -> None:
    bot = make_bot(api, acme_admin)
    api.put("/admin/settings/llm-key", headers=acme_admin, json={"api_key": "sk-acme-own-key-1234"})
    with get_session_factory()() as db:
        db.execute(update(Tenant).where(Tenant.slug == "acme").values(llm_api_key_encrypted="garbage"))
        db.commit()
    tenant_llm._cache.clear()
    failed = bot.send("hello", end_user=USER)
    assert failed.status_code == 503 and error(failed) == "model_unavailable"


def test_the_http_client_uses_the_workspace_key_when_one_is_bound(monkeypatch) -> None:
    from app.core.config import get_settings as settings_factory

    monkeypatch.setattr(llm, "_post", REAL_POST)
    monkeypatch.setenv("LLM_API_KEY", "platform-key")
    settings_factory.cache_clear()
    try:
        ok = response(200, {"choices": [{"message": {"content": "{}"}}], "usage": {"total_tokens": 3}})
        with patch("app.core.llm.httpx.post", return_value=ok) as post:
            llm.bind_usage(llm.new_usage("workspace-key"))
            llm.chat_completion([{"role": "user", "content": "hi"}])
            assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer workspace-key"
            llm.bind_usage(llm.new_usage())
            llm.chat_completion([{"role": "user", "content": "hi"}])
            assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer platform-key"
    finally:
        llm._usage.set(None)
        settings_factory.cache_clear()


def test_a_workspace_key_works_even_when_the_platform_has_none(monkeypatch) -> None:
    from app.core.config import get_settings as settings_factory

    monkeypatch.setattr(llm, "_post", REAL_POST)
    monkeypatch.setenv("LLM_API_KEY", "")
    settings_factory.cache_clear()
    try:
        with patch(
            "app.core.llm.httpx.post",
            return_value=response(200, {"data": [{"index": 0, "embedding": [0.0]}]}),
        ):
            llm.bind_usage(llm.new_usage())
            with pytest.raises(LLMUnavailable):
                llm.embed(["x"])
            llm.bind_usage(llm.new_usage("workspace-key"))
            assert llm.embed(["x"]) == [[0.0]]
    finally:
        llm._usage.set(None)
        settings_factory.cache_clear()
