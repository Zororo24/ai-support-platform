"""Runs an action: validate arguments, enforce guards, render the request, call the company's
API safely, and shape the answer for the model.

Policy (guards, argument validation, confirmation) is enforced here in code. The model can ask
for anything; this module decides what actually happens.
"""

import base64
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import jsonschema

from app.agents.action_schema import ActionDef, to_json_schema
from app.agents.expressions import Expression, lookup, parse, split_path
from app.agents.templating import TemplateError, render, render_json
from app.core import ssrf

logger = logging.getLogger(__name__)

USER_AGENT = "AgentPlatform/1.0"
RETRYABLE_STATUS = {502, 503, 504}
_SENSITIVE_KEY = re.compile(r"pass(?!age)|secret|token|authorization|api[_-]?key|cookie|credential", re.I)


class ActionRefused(Exception):
    """The call was rejected before being sent. `code` is stable, `message` is safe to show."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class ConnectionInfo:
    """What the executor needs about a connection. The secret is decrypted just-in-time."""

    base_url: str
    auth_type: str = "none"  # none | bearer | header | basic
    auth_header_name: str | None = None
    secret: str | None = None


@dataclass
class RenderedRequest:
    method: str
    url: str
    headers: dict[str, str]
    json_body: Any = None
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class ActionResult:
    ok: bool
    status_code: int | None = None
    data: Any = None
    error_code: str | None = None
    error: str | None = None
    latency_ms: int = 0
    truncated: bool = False


_client_override: httpx.Client | None = None


def set_client_override(client: httpx.Client | None) -> None:
    """Tests (and the demo) route action calls through a specific HTTP client."""
    global _client_override
    _client_override = client


def client_override() -> httpx.Client | None:
    return _client_override


# ---- before sending ---------------------------------------------------------------------------


def validate_args(action: ActionDef, args: Any) -> dict[str, Any]:
    if not isinstance(args, dict):
        raise ActionRefused("invalid_arguments", "arguments must be an object")
    try:
        jsonschema.validate(args, to_json_schema(action))
    except jsonschema.ValidationError as exc:
        where = ".".join(str(p) for p in exc.path)
        raise ActionRefused("invalid_arguments", f"{where + ': ' if where else ''}{exc.message}") from None
    return args


_guard_cache: dict[str, Expression] = {}


def check_guards(action: ActionDef, args: dict[str, Any], context: dict[str, Any]) -> None:
    scope = {"args": args, "context": context}
    for guard in action.guards:
        expr = _guard_cache.get(guard.when) or _guard_cache.setdefault(guard.when, parse(guard.when))
        if expr.evaluate(scope):
            raise ActionRefused("guard_refused", guard.message)


def render_request(
    action: ActionDef,
    args: dict[str, Any],
    context: dict[str, Any],
    connection: ConnectionInfo,
    *,
    idempotency_key: str | None = None,
) -> RenderedRequest:
    request = action.request
    if request is None:  # only hand-back actions lack one, and those are never called from here
        raise ActionRefused("not_callable", "this action is performed by your backend, not called by us")
    scope = {"args": args, "context": context}
    try:
        path = render(request.path, scope, url_quote=True)
        params = {k: render(v, scope) for k, v in request.query.items()}
        body = render_json(request.body, scope) if request.body is not None else None
        headers = {k: str(render(v, scope)) for k, v in request.headers.items()}
    except TemplateError as exc:
        if exc.missing and exc.missing.startswith("context."):
            raise ActionRefused(
                "missing_context",
                f"this action needs '{exc.missing}' from the end user's attributes, but it was not provided",
            ) from None
        raise ActionRefused("invalid_arguments", str(exc)) from None
    headers.setdefault("Accept", "application/json")
    headers["User-Agent"] = USER_AGENT
    if idempotency_key and action.idempotency_header:
        headers[action.idempotency_header] = idempotency_key
    headers.update(_auth_headers(connection))
    # A path that still looks like it escapes the base (encoded dots are fine) is refused outright.
    base = connection.base_url.rstrip("/")
    return RenderedRequest(
        method=request.method, url=base + path, headers=headers, json_body=body, params=params
    )


def _auth_headers(connection: ConnectionInfo) -> dict[str, str]:
    secret = connection.secret
    if not secret or connection.auth_type == "none":
        return {}
    if connection.auth_type == "bearer":
        return {"Authorization": f"Bearer {secret}"}
    if connection.auth_type == "header" and connection.auth_header_name:
        return {connection.auth_header_name: secret}
    if connection.auth_type == "basic":
        return {"Authorization": "Basic " + base64.b64encode(secret.encode()).decode()}
    return {}


# ---- sending -----------------------------------------------------------------------------------


def _pinned(target: ssrf.Target, request: RenderedRequest) -> tuple[str, dict[str, str], dict[str, Any]]:
    """Connect to the IP we validated while presenting the real hostname (Host header + TLS SNI)."""
    host_header = target.host if target.port in (80, 443) else f"{target.host}:{target.port}"
    ip = f"[{target.ip}]" if ":" in target.ip else target.ip
    url = f"{target.scheme}://{ip}:{target.port}{target.path_and_query.split('?')[0]}"
    headers = {**request.headers, "Host": host_header}
    extensions = {"sni_hostname": target.host} if target.scheme == "https" else {}
    return url, headers, extensions


def _single_attempt(client: httpx.Client, action: ActionDef, request: RenderedRequest) -> ActionResult:
    started = time.perf_counter()
    cap = action.limits.max_response_bytes

    def done(**kwargs: Any) -> ActionResult:
        return ActionResult(latency_ms=round((time.perf_counter() - started) * 1000), **kwargs)

    try:
        target = ssrf.validate_url(request.url)
    except ssrf.BlockedDestination as exc:
        return done(ok=False, error_code="blocked_destination", error=str(exc))
    url, headers, extensions = _pinned(target, request)
    try:
        with client.stream(
            request.method,
            url,
            headers=headers,
            params=request.params or None,
            json=request.json_body,
            timeout=action.limits.timeout_s,
            extensions=extensions,
        ) as response:
            raw = b""
            truncated = False
            for chunk in response.iter_bytes():
                raw += chunk
                if len(raw) > cap:
                    raw, truncated = raw[:cap], True
                    break
            status = response.status_code
            content_type = response.headers.get("content-type", "")
    except httpx.TimeoutException:
        return done(ok=False, error_code="timeout", error=f"no response within {action.limits.timeout_s}s")
    except httpx.HTTPError:
        return done(ok=False, error_code="unreachable", error="could not reach the service")

    if 300 <= status < 400:
        return done(
            ok=False, status_code=status, error_code="redirect", error="the service redirected the request"
        )
    data: Any = None
    if raw:
        if truncated:
            return done(
                ok=False, status_code=status, error_code="response_too_large",
                error=f"the response is larger than {cap} bytes", truncated=True,
            )  # fmt: skip
        if "json" in content_type.lower() or raw[:1] in (b"{", b"["):
            try:
                data = json.loads(raw)
            except ValueError:
                if 200 <= status < 300:
                    return done(
                        ok=False,
                        status_code=status,
                        error_code="invalid_response",
                        error="the service returned invalid JSON",
                    )
        elif 200 <= status < 300:
            return done(
                ok=False,
                status_code=status,
                error_code="invalid_response",
                error="the service did not return JSON",
            )
    if not 200 <= status < 300:
        detail = None
        if isinstance(data, dict):
            detail = next((str(data[k])[:200] for k in ("message", "error", "detail") if k in data), None)
        return done(
            ok=False, status_code=status, data=None, error_code="http_error",
            error=f"the service answered with status {status}" + (f": {detail}" if detail else ""),
        )  # fmt: skip
    return done(ok=True, status_code=status, data=data)


def call(action: ActionDef, request: RenderedRequest, *, client: httpx.Client | None = None) -> ActionResult:
    """Send the request. Read actions are retried on transient failures; writes never are."""
    own = client is None
    http = client or httpx.Client(follow_redirects=False)
    try:
        attempts = 1 + (action.limits.retries if action.effect == "read" else 0)
        result = ActionResult(ok=False, error_code="unreachable", error="no attempt was made")
        for attempt in range(attempts):
            result = _single_attempt(http, action, request)
            transient = (
                result.error_code in ("timeout", "unreachable") or result.status_code in RETRYABLE_STATUS
            )
            if result.ok or not transient or attempt == attempts - 1:
                break
            time.sleep(0.2 * (attempt + 1))
        return result
    finally:
        if own:
            http.close()


# ---- after receiving ---------------------------------------------------------------------------


def _pick(data: Any, path: str) -> Any:
    source = path[2:] if path.startswith("$.") else path.lstrip("$").lstrip(".")
    segments = split_path(source)
    return lookup(data, segments) if segments else None


def shape_for_model(action: ActionDef, result: ActionResult) -> dict[str, Any]:
    """What the model is told about the outcome: only exposed fields, size-limited."""
    if not result.ok:
        return {"ok": False, "error": result.error_code, "message": result.error}
    data = result.data
    if action.response.expose and data is not None:
        picked: dict[str, Any] = {}
        for path in action.response.expose:
            key = path.rstrip("]").split(".")[-1].split("[")[0] or path
            picked[key if key not in picked else path] = _pick(data, path)
        data = picked
    text = json.dumps(data, default=str)
    limit = action.response.max_chars
    if len(text) > limit:
        return {"ok": True, "data": text[:limit], "truncated": True}
    return {"ok": True, "data": data}


def redact(value: Any, *, depth: int = 0) -> Any:
    """Copy of `value` that is safe to store or log: credentials masked, long text cut."""
    if depth > 8:
        return "…"
    if isinstance(value, dict):
        return {
            k: "[redacted]" if isinstance(k, str) and _SENSITIVE_KEY.search(k) else redact(v, depth=depth + 1)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v, depth=depth + 1) for v in value[:50]]
    if isinstance(value, str) and len(value) > 2000:
        return value[:2000] + "…"
    return value


def confirmation_summary(action: ActionDef, args: dict[str, Any], context: dict[str, Any]) -> str:
    """The sentence the end user is asked to approve. Deterministic: rendered from the action's own
    template (or a plain description of the call), never written by the model."""
    scope = {"args": args, "context": context}
    if action.confirm_summary:
        try:
            return str(render(action.confirm_summary, scope))
        except TemplateError:
            pass
    shown = ", ".join(f"{k}={json.dumps(v)}" for k, v in args.items())
    return f"{action.description.rstrip('.')} ({shown})" if shown else action.description.rstrip(".")
