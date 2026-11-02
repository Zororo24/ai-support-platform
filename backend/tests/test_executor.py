"""The HTTP action executor, against a mock transport (no network)."""

import json

import httpx
import pytest

from app.agents import executor
from app.agents.action_schema import ActionDef
from app.agents.executor import ActionRefused, ConnectionInfo
from app.core import ssrf

CONN_ID = "00000000-0000-0000-0000-000000000001"
SECRET = "sk-company-secret-9999"


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    monkeypatch.setattr(
        ssrf,
        "resolve_host",
        lambda host: {"api.acme.test": ["8.8.8.8"], "evil.test": ["10.0.0.7"]}.get(host, []),
    )
    monkeypatch.setattr(executor.time, "sleep", lambda s: None)


def connection(**kw) -> ConnectionInfo:
    return ConnectionInfo(base_url="https://api.acme.test/v1", auth_type="bearer", secret=SECRET, **kw)


def action(**overrides) -> ActionDef:
    base = {
        "name": "get_order",
        "description": "Look up an order.",
        "connection_id": CONN_ID,
        "request": {
            "method": "GET",
            "path": "/orders/{{args.order_number}}",
            "query": {"customer_id": "{{context.customer_id}}"},
        },
        "parameters": {"order_number": {"type": "string", "required": True, "pattern": "^[A-Z]{3}-[0-9]+$"}},
    }
    base.update(overrides)
    return ActionDef.model_validate(base)


def client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)


def run(act: ActionDef, handler, args=None, context=None, conn=None):
    rendered = executor.render_request(
        act, args or {"order_number": "ACM-1"}, context or {"customer_id": "c1"}, conn or connection()
    )
    return executor.call(act, rendered, client=client(handler))


# ---------- rendering ----------


def test_request_is_rendered_with_quoting_auth_and_defaults() -> None:
    rendered = executor.render_request(
        action(), {"order_number": "ACM-1/../x"}, {"customer_id": "c 1"}, connection()
    )
    assert rendered.url == "https://api.acme.test/v1/orders/ACM-1%2F..%2Fx"
    assert rendered.params == {"customer_id": "c 1"}
    assert rendered.headers["Authorization"] == f"Bearer {SECRET}"
    assert rendered.headers["Accept"] == "application/json" and rendered.headers["User-Agent"].startswith(
        "AgentPlatform"
    )


def test_body_keeps_types_and_idempotency_header_is_added() -> None:
    act = action(
        name="create_return",
        effect="write",
        request={
            "method": "POST",
            "path": "/returns",
            "body": {"order": "{{args.order_number}}", "n": "{{args.qty}}", "who": "{{context.customer_id}}"},
        },
        parameters={"order_number": {"type": "string", "required": True}, "qty": {"type": "integer"}},
        idempotency_header="Idempotency-Key",
    )
    rendered = executor.render_request(
        act, {"order_number": "A-1", "qty": 2}, {"customer_id": "c1"}, connection(), idempotency_key="run:1"
    )
    assert rendered.json_body == {"order": "A-1", "n": 2, "who": "c1"}
    assert rendered.headers["Idempotency-Key"] == "run:1"


@pytest.mark.parametrize(
    "auth,expected",
    [
        (ConnectionInfo("https://api.acme.test", "none"), {}),
        (ConnectionInfo("https://api.acme.test", "header", "X-Api-Key", "k123"), {"X-Api-Key": "k123"}),
        (ConnectionInfo("https://api.acme.test", "basic", None, "u:p"), {"Authorization": "Basic dTpw"}),
    ],
)
def test_auth_styles(auth, expected) -> None:
    headers = executor.render_request(action(), {"order_number": "A-1"}, {"customer_id": "c"}, auth).headers
    assert {k: v for k, v in headers.items() if k not in ("Accept", "User-Agent")} == expected


def test_missing_context_value_is_reported_by_name() -> None:
    with pytest.raises(ActionRefused) as exc:
        executor.render_request(action(), {"order_number": "A-1"}, {}, connection())
    assert exc.value.code == "missing_context" and "context.customer_id" in exc.value.message


# ---------- arguments and guards ----------


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"order_number": 5},
        {"order_number": "bad"},
        {"order_number": "ACM-1", "customer_id": "other"},
        "text",
        None,
    ],
)
def test_arguments_are_validated_strictly(args) -> None:
    with pytest.raises(ActionRefused) as exc:
        executor.validate_args(action(), args)
    assert exc.value.code == "invalid_arguments"


def test_valid_arguments_pass_through() -> None:
    assert executor.validate_args(action(), {"order_number": "ACM-1"}) == {"order_number": "ACM-1"}


def test_guards_refuse_with_the_configured_message() -> None:
    act = action(
        parameters={"order_number": {"type": "string"}, "amount": {"type": "integer"}},
        guards=[
            {"when": "args.amount > 100 or context.plan == 'free'", "message": "Too large for self-service."}
        ],
        request={"method": "GET", "path": "/o/{{args.order_number}}"},
    )
    executor.check_guards(act, {"amount": 50}, {"plan": "pro"})
    with pytest.raises(ActionRefused) as exc:
        executor.check_guards(act, {"amount": 500}, {"plan": "pro"})
    assert exc.value.code == "guard_refused" and exc.value.message == "Too large for self-service."
    with pytest.raises(ActionRefused):
        executor.check_guards(act, {"amount": 5}, {"plan": "free"})


# ---------- sending ----------


def test_request_is_pinned_to_the_validated_ip_with_the_real_host_header() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["host"], seen["url_host"] = request.headers["host"], request.url.host
        seen["auth"], seen["query"], seen["path"] = (
            request.headers["authorization"],
            dict(request.url.params),
            request.url.path,
        )
        return httpx.Response(200, json={"status": "shipped"})

    result = run(action(), handler)
    assert result.ok and result.data == {"status": "shipped"} and result.status_code == 200
    assert seen == {
        "host": "api.acme.test",
        "url_host": "8.8.8.8",
        "auth": f"Bearer {SECRET}",
        "query": {"customer_id": "c1"},
        "path": "/v1/orders/ACM-1",
    }


def test_private_destination_is_blocked_before_any_request() -> None:
    hits = []
    result = run(
        action(),
        lambda r: hits.append(r) or httpx.Response(200, json={}),
        conn=ConnectionInfo("https://evil.test/v1", "none"),
    )
    assert not result.ok and result.error_code == "blocked_destination" and hits == []
    assert "10.0.0.7" not in (result.error or "")


@pytest.mark.parametrize(
    "response,code",
    [
        (httpx.Response(404, json={"message": "no such order"}), "http_error"),
        (httpx.Response(500, text="boom"), "http_error"),
        (httpx.Response(302, headers={"location": "https://evil.test/"}), "redirect"),
        (
            httpx.Response(200, text="<html>hi</html>", headers={"content-type": "text/html"}),
            "invalid_response",
        ),
        (
            httpx.Response(200, content=b"{broken", headers={"content-type": "application/json"}),
            "invalid_response",
        ),
    ],
)
def test_failures_are_mapped_to_stable_codes(response, code) -> None:
    result = run(action(), lambda r: response)
    assert not result.ok and result.error_code == code


def test_http_error_includes_a_short_service_message() -> None:
    result = run(action(), lambda r: httpx.Response(404, json={"message": "no such order"}))
    assert "404" in result.error and "no such order" in result.error


def test_timeouts_and_connection_errors() -> None:
    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    def refused(request):
        raise httpx.ConnectError("refused", request=request)

    assert run(action(), timeout).error_code == "timeout"
    assert run(action(), refused).error_code == "unreachable"


def test_oversized_responses_are_cut_off() -> None:
    act = action(limits={"max_response_bytes": 1024})
    result = run(
        act,
        lambda r: httpx.Response(
            200, content=b'{"x":"' + b"a" * 5000 + b'"}', headers={"content-type": "application/json"}
        ),
    )
    assert not result.ok and result.error_code == "response_too_large" and result.truncated


def test_empty_success_is_fine() -> None:
    result = run(action(), lambda r: httpx.Response(204))
    assert result.ok and result.data is None


def test_reads_retry_transient_failures_but_writes_never_do() -> None:
    attempts = {"n": 0}

    def flaky(request):
        attempts["n"] += 1
        return httpx.Response(503) if attempts["n"] < 3 else httpx.Response(200, json={"ok": 1})

    assert run(action(limits={"retries": 2}), flaky).ok and attempts["n"] == 3

    calls = {"n": 0}

    def always_503(request):
        calls["n"] += 1
        return httpx.Response(503)

    write = action(
        name="make", effect="write", limits={"retries": 2},
        request={"method": "POST", "path": "/o", "body": {"a": "{{args.order_number}}"}},
    )  # fmt: skip
    assert not run(write, always_503).ok and calls["n"] == 1  # no retry: could duplicate a side effect


# ---------- what the model sees, and what we store ----------


def test_model_view_exposes_only_chosen_fields_and_never_secrets() -> None:
    act = action(response={"expose": ["$.order.status", "$.items[0].name", "$.missing.path"]})
    result = run(
        act,
        lambda r: httpx.Response(
            200,
            json={
                "order": {"status": "shipped", "internal_cost": 3},
                "items": [{"name": "headphones"}],
                "token": SECRET,
            },
        ),
    )
    shaped = executor.shape_for_model(act, result)
    assert shaped == {"ok": True, "data": {"status": "shipped", "name": "headphones", "path": None}}
    assert SECRET not in json.dumps(shaped) and "internal_cost" not in json.dumps(shaped)


def test_long_responses_are_truncated_for_the_model() -> None:
    act = action(response={"max_chars": 200})
    result = executor.ActionResult(ok=True, data={"text": "x" * 5000})
    shaped = executor.shape_for_model(act, result)
    assert shaped["truncated"] is True and len(shaped["data"]) == 200


def test_failures_are_described_without_internals() -> None:
    shaped = executor.shape_for_model(
        action(), executor.ActionResult(ok=False, error_code="timeout", error="no response within 10s")
    )
    assert shaped == {"ok": False, "error": "timeout", "message": "no response within 10s"}


def test_redaction_masks_credentials_and_cuts_long_text() -> None:
    cleaned = executor.redact(
        {
            "authorization": "Bearer x",
            "api_key": "k",
            "nested": {"Password": "p", "ok": "fine"},
            "long": "y" * 5000,
            "items": [{"token": "t"}],
        }
    )
    assert (
        cleaned["authorization"]
        == cleaned["api_key"]
        == cleaned["nested"]["Password"]
        == cleaned["items"][0]["token"]
        == "[redacted]"
    )
    assert cleaned["nested"]["ok"] == "fine" and len(cleaned["long"]) < 2100


def test_confirmation_summary_uses_the_template_or_a_plain_description() -> None:
    templated = action(confirm_summary="Return order {{args.order_number}} for {{context.customer_id}}")
    assert (
        executor.confirmation_summary(templated, {"order_number": "A-1"}, {"customer_id": "c1"})
        == "Return order A-1 for c1"
    )
    plain = executor.confirmation_summary(action(), {"order_number": "A-1"}, {})
    assert plain == 'Look up an order (order_number="A-1")'


def test_redaction_masks_credentials_but_not_search_passages() -> None:
    from app.agents.executor import redact

    masked = redact({"password": "x", "passwd": "x", "api_key": "x", "passages": [{"title": "Returns"}]})
    assert masked["password"] == masked["passwd"] == masked["api_key"] == "[redacted]"
    assert masked["passages"] == [{"title": "Returns"}]
