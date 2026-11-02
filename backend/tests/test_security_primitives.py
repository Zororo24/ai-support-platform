"""SSRF guard, secrets vault, and action schema validation."""

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from app.agents.action_schema import ActionDef, to_json_schema
from app.core import secrets, ssrf
from app.core.config import get_settings

# ---------- SSRF ----------


@pytest.mark.parametrize(
    "ip,public",
    [
        ("8.8.8.8", True),
        ("1.1.1.1", True),
        ("2606:4700:4700::1111", True),
        ("127.0.0.1", False),
        ("10.0.0.5", False),
        ("172.16.4.4", False),
        ("192.168.1.1", False),
        ("169.254.169.254", False),  # cloud metadata
        ("100.64.0.1", False),  # shared address space
        ("0.0.0.0", False),
        ("224.0.0.1", False),
        ("::1", False),
        ("fe80::1", False),
        ("fc00::1", False),
        ("::ffff:127.0.0.1", False),  # IPv4-mapped loopback
        ("::ffff:10.0.0.1", False),
    ],
)
def test_address_classification(ip, public) -> None:
    assert ssrf.is_public(ip) is public


@pytest.fixture
def resolver(monkeypatch):
    table: dict[str, list[str]] = {}
    monkeypatch.setattr(ssrf, "resolve_host", lambda host: table.get(host, []))
    return table


@pytest.mark.parametrize(
    "url,reason",
    [
        ("http://api.example.test/x", "https"),
        ("ftp://api.example.test/x", "https"),
        ("https://user:pw@api.example.test/x", "credentials"),
        ("https://api.example.test:8443/x", "port"),
        ("https://127.0.0.1/x", "publicly reachable"),
        ("https://[::1]/x", "publicly reachable"),
        ("https://169.254.169.254/latest/meta-data", "publicly reachable"),
        ("https://localhost-alias.test/x", "publicly reachable"),  # resolves to loopback (below)
        ("https://rebind.test/x", "publicly reachable"),  # one public + one private answer
        ("https://nowhere.test/x", "cannot resolve"),
        ("https:///x", "no host"),
    ],
)
def test_blocked_urls(resolver, url, reason) -> None:
    resolver.update(
        {
            "api.example.test": ["8.8.8.8"],
            "localhost-alias.test": ["127.0.0.1"],
            "rebind.test": ["8.8.8.8", "10.0.0.9"],
        }
    )
    with pytest.raises(ssrf.BlockedDestination) as exc:
        ssrf.validate_url(url)
    assert reason in str(exc.value)
    assert "10.0.0.9" not in str(exc.value) and "127.0.0.1" not in str(exc.value)  # no address leaks


def test_public_url_is_accepted_and_pinned(resolver) -> None:
    resolver["api.example.test"] = ["8.8.4.4", "8.8.8.8"]
    target = ssrf.validate_url("https://api.example.test/v1/orders?x=1")
    assert (target.scheme, target.host, target.port, target.ip) == (
        "https",
        "api.example.test",
        443,
        "8.8.4.4",
    )
    assert target.path_and_query == "/v1/orders?x=1"


def test_private_destinations_only_with_the_explicit_dev_switch(resolver, monkeypatch) -> None:
    resolver["intranet.test"] = ["10.1.2.3"]
    monkeypatch.setattr(get_settings(), "allow_private_connections", True)
    target = ssrf.validate_url("http://intranet.test:8000/x")
    assert target.ip == "10.1.2.3" and target.port == 8000


# ---------- secrets ----------


def test_encrypt_decrypt_roundtrip_and_hint() -> None:
    token = secrets.encrypt("sk-very-secret-1234")
    assert "sk-very-secret" not in token
    assert secrets.decrypt(token) == "sk-very-secret-1234"
    assert secrets.encrypt("sk-very-secret-1234") != token  # random IV each time
    assert secrets.hint("sk-very-secret-1234") == "…1234" and secrets.hint("short") == "••••"


def test_key_rotation_keeps_old_secrets_readable(monkeypatch) -> None:
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    settings = get_settings()
    monkeypatch.setattr(settings, "secrets_key", old)
    token = secrets.encrypt("hello")
    monkeypatch.setattr(settings, "secrets_key", f"{new},{old}")  # new first, old kept for reading
    assert secrets.decrypt(token) == "hello"
    assert secrets.decrypt(secrets.encrypt("again")) == "again"
    monkeypatch.setattr(settings, "secrets_key", new)  # old key dropped
    with pytest.raises(secrets.SecretError):
        secrets.decrypt(token)


def test_tampered_secret_is_rejected() -> None:
    token = secrets.encrypt("hello")
    with pytest.raises(secrets.SecretError):
        secrets.decrypt(token[:-4] + "AAAA")


# ---------- action schema ----------

CONN = "00000000-0000-0000-0000-000000000001"


def action(**overrides):
    base = {
        "name": "get_order",
        "description": "Look up an order.",
        "connection_id": CONN,
        "request": {
            "method": "GET",
            "path": "/orders/{{args.order_number}}",
            "query": {"c": "{{context.customer_id}}"},
        },
        "parameters": {"order_number": {"type": "string", "required": True, "pattern": "^[A-Z]{3}-[0-9]+$"}},
    }
    base.update(overrides)
    return base


def test_valid_action_and_defaults() -> None:
    a = ActionDef.model_validate(action())
    assert a.effect == "read" and a.effective_confirmation == "none"
    write = ActionDef.model_validate(
        action(
            effect="write", request={"method": "POST", "path": "/r", "body": {"o": "{{args.order_number}}"}}
        )
    )
    assert write.effective_confirmation == "user"  # writes ask by default
    explicit = ActionDef.model_validate(
        action(effect="write", confirmation="none", request={"method": "POST", "path": "/r"})
    )
    assert explicit.effective_confirmation == "none"


@pytest.mark.parametrize(
    "overrides,message",
    [
        ({"name": "Bad Name"}, "lowercase"),
        ({"request": {"method": "GET", "path": "orders"}}, "start with"),
        ({"request": {"method": "GET", "path": "/a/../b"}}, "'..'"),
        ({"request": {"method": "GET", "path": "/a?x=1"}}, "query"),
        ({"request": {"method": "GET", "path": "/o/{{args.nope}}"}}, "not declared"),
        ({"request": {"method": "GET", "path": "/o/{{vars.x}}"}}, "args. or context."),
        ({"request": {"method": "GET", "path": "/o/{{args.order_number + 1}}"}}, "plain variable path"),
        ({"request": {"method": "GET", "path": "/o", "headers": {"Authorization": "x"}}}, "platform"),
        ({"request": {"method": "GET", "path": "/o", "headers": {"Host": "evil"}}}, "platform"),
        ({"request": {"method": "GET", "path": "/o", "body": {"a": 1}}}, "GET"),
        ({"guards": [{"when": "args.order_number >"}]}, "guard"),
        ({"guards": [{"when": "args.ghost > 1"}]}, "not a declared"),
        ({"guards": [{"when": "vars.x > 1"}]}, "args. or context."),
        ({"idempotency_header": "Idempotency-Key"}, "write"),
        ({"parameters": {"Bad": {"type": "string"}}}, "lowercase"),
        ({"parameters": {"x": {"type": "string", "pattern": "(unclosed"}}}, "pattern"),
        ({"surprise": 1}, "Extra inputs"),
        ({"limits": {"timeout_s": 999}}, "less than or equal"),
    ],
)
def test_invalid_actions_are_rejected_with_a_reason(overrides, message) -> None:
    with pytest.raises(ValidationError) as exc:
        ActionDef.model_validate(action(**overrides))
    assert message in str(exc.value)


def test_json_schema_is_strict() -> None:
    schema = to_json_schema(
        ActionDef.model_validate(
            action(
                parameters={
                    "n": {"type": "integer", "minimum": 1, "maximum": 5, "required": True},
                    "s": {"type": "string", "enum": ["a", "b"]},
                },
                request={"method": "GET", "path": "/o/{{args.n}}"},
            )
        )
    )
    assert schema["additionalProperties"] is False and schema["required"] == ["n"]
    assert schema["properties"]["n"] == {"type": "integer", "minimum": 1, "maximum": 5}
    assert schema["properties"]["s"]["enum"] == ["a", "b"] and schema["properties"]["s"]["maxLength"] == 2000
