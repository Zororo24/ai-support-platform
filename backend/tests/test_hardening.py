import json
from datetime import datetime, timedelta, timezone

import jwt
import pytest
import redis

from app.core import ratelimit
from app.core.config import get_settings
from app.core.llm import LLMUnavailable
from tests.conftest import PASSWORD, login

GOOD = "What is the return window for returns?"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def raw_login(api, email, password=PASSWORD):
    return api.post("/auth/login", json={"email": email, "password": password})


# ---------- refresh / revocation ----------


def test_refresh_issues_a_working_fresh_token(api, acme_user) -> None:
    response = api.post("/auth/refresh", headers=acme_user)
    assert response.status_code == 200
    body = response.json()
    assert body["user"]["email"] == "acme.user@example.com"
    assert api.get("/auth/me", headers=bearer(body["access_token"])).status_code == 200
    assert api.get("/auth/me", headers=acme_user).status_code == 200  # sliding: old one lives on


def test_expired_or_garbage_token_cannot_refresh(api, acme_user) -> None:
    me = api.get("/auth/me", headers=acme_user).json()
    expired = jwt.encode(
        {
            "sub": me["id"],
            "tenant_id": me["tenant_id"],
            "role": "customer",
            "tv": 0,
            "exp": datetime.now(timezone.utc) - timedelta(minutes=1),
        },
        get_settings().jwt_secret,
        algorithm="HS256",
    )
    assert api.post("/auth/refresh", headers=bearer(expired)).status_code == 401
    assert api.post("/auth/refresh", headers=bearer("nonsense")).status_code == 401
    assert api.post("/auth/refresh").status_code == 401


def test_logout_all_revokes_every_session_of_that_user_only(api, acme_user, acme_admin) -> None:
    second_device = login(api, "acme.user@example.com")
    assert api.post("/auth/logout-all", headers=acme_user).status_code == 204
    assert api.get("/auth/me", headers=acme_user).status_code == 401
    assert api.get("/auth/me", headers=second_device).status_code == 401
    assert api.post("/auth/refresh", headers=acme_user).status_code == 401  # cannot refresh a revoked token
    assert api.get("/auth/me", headers=acme_admin).status_code == 200
    assert raw_login(api, "acme.user@example.com").status_code == 200  # signing in again works


def test_admin_password_reset_signs_the_user_out_but_role_change_does_not(api, acme_user, acme_admin) -> None:
    target = next(
        u for u in api.get("/admin/users", headers=acme_admin).json() if u["email"] == "acme.user@example.com"
    )
    api.patch(f"/admin/users/{target['id']}", headers=acme_admin, json={"role": "admin"})
    assert api.get("/auth/me", headers=acme_user).status_code == 200
    api.patch(f"/admin/users/{target['id']}", headers=acme_admin, json={"password": "another-pass-1"})
    assert api.get("/auth/me", headers=acme_user).status_code == 401


def test_tokens_issued_before_versioning_still_work(api, acme_user) -> None:
    me = api.get("/auth/me", headers=acme_user).json()
    legacy = jwt.encode(
        {  # no "tv" claim
            "sub": me["id"],
            "tenant_id": me["tenant_id"],
            "role": "customer",
            "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        },
        get_settings().jwt_secret,
        algorithm="HS256",
    )
    assert api.get("/auth/me", headers=bearer(legacy)).status_code == 200


# ---------- rate limiting ----------


def chat(api, headers, text="hello"):
    return api.post("/chat/messages", headers=headers, json={"content": text})


def test_chat_is_limited_per_user_with_retry_after(api, acme_user, acme_admin, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "chat_rate_per_user", 3)
    assert [chat(api, acme_user).status_code for _ in range(3)] == [200, 200, 200]
    blocked = chat(api, acme_user)
    assert blocked.status_code == 429
    assert 1 <= int(blocked.headers["Retry-After"]) <= 60
    assert "Too many requests" in blocked.json()["detail"]
    assert chat(api, acme_admin).status_code == 200  # a different user is unaffected


def test_chat_is_limited_per_tenant_and_tenants_are_independent(
    api, acme_user, acme_admin, globex_user, monkeypatch
) -> None:
    monkeypatch.setattr(get_settings(), "chat_rate_per_tenant", 3)
    assert chat(api, acme_user).status_code == 200
    assert chat(api, acme_admin).status_code == 200
    assert chat(api, acme_user).status_code == 200
    assert chat(api, acme_admin).status_code == 429  # tenant budget used up by two different people
    assert chat(api, globex_user).status_code == 200


def test_limiter_fails_open_when_redis_is_down(api, acme_user, monkeypatch) -> None:
    def broken():
        raise redis.ConnectionError("redis is down")

    monkeypatch.setattr(ratelimit, "get_redis", broken)
    monkeypatch.setattr(get_settings(), "chat_rate_per_user", 1)
    assert [chat(api, acme_user).status_code for _ in range(3)] == [200, 200, 200]


def test_login_brute_force_is_throttled(api, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "login_attempts_per_email", 3)
    attempts = [raw_login(api, "acme.user@example.com", "wrong").status_code for _ in range(3)]
    assert attempts == [401, 401, 401]
    locked = raw_login(api, "acme.user@example.com", PASSWORD)  # even the right password
    assert locked.status_code == 429 and "Retry-After" in locked.headers
    assert raw_login(api, "acme.admin@example.com").status_code == 200  # other accounts unaffected


def test_ingest_and_retrieval_tester_are_throttled(api, acme_admin, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "ingest_rate_per_user", 2)
    monkeypatch.setattr(get_settings(), "retrieval_test_rate_per_user", 1)
    codes = [
        api.post(
            "/admin/documents", headers=acme_admin, json={"title": f"t{i}", "content": f"text {i}"}
        ).status_code
        for i in range(3)
    ]
    assert codes == [201, 201, 429]
    assert api.post("/admin/retrieval/test", headers=acme_admin, json={"query": "returns"}).status_code == 200
    assert api.post("/admin/retrieval/test", headers=acme_admin, json={"query": "returns"}).status_code == 429


# ---------- SSE streaming ----------


def parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.strip().split("\n\n"):
        lines = block.split("\n")
        name = lines[0].removeprefix("event: ")
        data = json.loads(lines[1].removeprefix("data: "))
        events.append((name, data))
    return events


def stream(api, headers, text, conversation_id=None):
    return api.post(
        "/chat/messages/stream", headers=headers, json={"content": text, "conversation_id": conversation_id}
    )


def test_stream_emits_truthful_stages_then_the_full_result(api, acme_admin) -> None:
    response = stream(api, acme_admin, GOOD)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(response.text)
    names = [name for name, _ in events]
    assert names == ["stage", "stage", "stage", "done"]
    assert [d["node"] for n, d in events if n == "stage"] == ["classify", "retrieve", "generate"]
    labels = [d["label"] for n, d in events if n == "stage"]
    assert labels[0].startswith("Understood your message as: knowledge")
    assert labels[1].startswith("Found ") and labels[2] == "Wrote an answer from your documents"

    done = events[-1][1]
    assert "30 days" in done["content"] and done["citations"]
    assert done["meta"]["total_tokens"] > 0  # usage is counted across resumed threads

    stored = api.get(f"/conversations/{done['conversation_id']}/messages", headers=acme_admin).json()
    assert [m["role"] for m in stored] == ["user", "assistant"]
    assert stored[-1]["meta"]["outcome"] == "answered"


def test_stream_matches_non_stream_behaviour_for_tools(api, acme_user) -> None:
    events = parse_sse(stream(api, acme_user, "Where is order ACM-1001?").text)
    assert [d["node"] for n, d in events if n == "stage"] == ["classify", "lookup_order"]
    done = events[-1][1]
    assert done["order"]["order_number"] == "ACM-1001"
    assert done["meta"] == {}  # customers still never see internals

    events = parse_sse(stream(api, acme_user, "I need a refund exception").text)
    assert events[-1][1]["ticket"]["id"]
    assert api.get("/tickets", headers=acme_user).json()["total"] == 1


def test_stream_errors_arrive_as_events_and_save_nothing(api, acme_user, fake_llm) -> None:
    from uuid import uuid4

    missing = parse_sse(stream(api, acme_user, "hi", str(uuid4())).text)
    assert missing == [("error", {"status": 404, "detail": "Conversation not found"})]

    def down(path, payload):
        raise LLMUnavailable("down")

    fake_llm.override = down
    events = parse_sse(stream(api, acme_user, "hi").text)
    assert events[-1][0] == "error" and events[-1][1]["status"] == 503
    fake_llm.override = None
    assert api.get("/conversations", headers=acme_user).json() == []


def test_stream_requires_auth_and_is_rate_limited(api, acme_user, monkeypatch) -> None:
    assert api.post("/chat/messages/stream", json={"content": "x"}).status_code == 401
    monkeypatch.setattr(get_settings(), "chat_rate_per_user", 1)
    assert stream(api, acme_user, "hello").status_code == 200
    assert stream(api, acme_user, "hello").status_code == 429  # a real status, before streaming starts


# ---------- migrations ----------


def test_alembic_upgrade_on_empty_database_matches_models() -> None:
    import os
    import subprocess
    import sys
    from pathlib import Path

    from sqlalchemy import create_engine, inspect, text
    from sqlalchemy.engine import make_url

    from app.db.base import Base

    try:
        url = make_url(os.environ["DATABASE_URL"])
        name = "support_migration_test"
        admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    except Exception as exc:
        pytest.skip(f"Postgres not available: {exc}")

    target = url.set(database=name)
    env = {**os.environ, "DATABASE_URL": target.render_as_string(hide_password=False)}
    backend_dir = Path(__file__).resolve().parents[1]
    for _ in range(2):  # the second run proves upgrade is a no-op, not an error
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=backend_dir,
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

    engine = create_engine(target)
    inspector = inspect(engine)
    missing = []
    for table in Base.metadata.tables.values():
        if not inspector.has_table(table.name):
            missing.append(table.name)
            continue
        existing = {c["name"] for c in inspector.get_columns(table.name)}
        missing += [f"{table.name}.{c.name}" for c in table.columns if c.name not in existing]
    with engine.connect() as conn:
        version = conn.scalar(text("SELECT version_num FROM alembic_version"))
    engine.dispose()
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
    admin.dispose()
    assert missing == []
    assert version == "0012"
