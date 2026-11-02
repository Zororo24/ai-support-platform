"""A thin Python client for the agent platform's /v1 API.

    from agent_platform import AgentPlatform

    agents = AgentPlatform(api_key="sk_live_...", agent_id="...", base_url="https://agents.example.com")
    reply = agents.send("Where is my order ACM-1002?", session_id="chat-123",
                        end_user_id="customer-42", end_user_attributes={"customer_id": "cust_42"})
    print(reply.status, reply.reply)

Run it on YOUR SERVER: the key is a secret and must never reach a browser or app. The only
dependency is httpx. Every method returns a `Reply`; failures raise `AgentPlatformError` with the
API's stable error `code` (for example `rate_limited`, `session_busy`, `budget_exceeded`).
"""

import hashlib
import hmac
import json
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx

__all__ = ["AgentPlatform", "AgentPlatformError", "Reply", "verify_webhook"]


class AgentPlatformError(Exception):
    def __init__(self, status_code: int, code: str, message: str, retry_after: int | None = None):
        super().__init__(f"{code}: {message}")
        self.status_code, self.code, self.message, self.retry_after = status_code, code, message, retry_after


@dataclass
class Reply:
    """What the agent answered. `status` says what your code has to do next:

    completed            show `reply`
    needs_input          the agent is collecting details; show `reply`, send the answer as the next message
    needs_confirmation   show `reply` with Yes/No; call `confirm(session_id, pending_confirmation["id"], ...)`
    pending_approval     staff must allow it; show `reply`, wait for the `approval.decided` webhook
    action_required      perform `action_request` with your own credentials, then `send_tool_result(...)`
    handed_off           route the customer to a person; see `handoff`
    failed               show `reply`
    """

    status: str
    reply: str
    session_id: str
    run_id: str | None = None
    outcome: str = ""
    citations: list[dict[str, Any]] = field(default_factory=list)
    actions_taken: list[dict[str, Any]] = field(default_factory=list)
    pending_confirmation: dict[str, Any] | None = None
    pending_approval: dict[str, Any] | None = None
    action_request: dict[str, Any] | None = None
    handoff: dict[str, Any] | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Reply":
        return cls(
            status=data["status"],
            reply=data["reply"],
            session_id=data["session_id"],
            run_id=data.get("run_id"),
            outcome=data.get("outcome", ""),
            citations=data.get("citations") or [],
            actions_taken=data.get("actions_taken") or [],
            pending_confirmation=data.get("pending_confirmation"),
            pending_approval=data.get("pending_approval"),
            action_request=data.get("action_request"),
            handoff=data.get("handoff"),
            raw=data,
        )


class AgentPlatform:
    def __init__(
        self,
        api_key: str,
        agent_id: str,
        base_url: str = "http://localhost:8000",
        *,
        timeout: float = 60.0,
        http: httpx.Client | None = None,
    ) -> None:
        self.agent_id = agent_id
        self._http = http or httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)
        self._headers = {"Authorization": f"Bearer {api_key}"}

    # ---- conversation ------------------------------------------------------------------------

    def send(
        self,
        message: str,
        *,
        session_id: str | None = None,
        end_user_id: str | None = None,
        end_user_attributes: dict[str, Any] | None = None,
        workflow: str | None = None,
        idempotency_key: str | None = None,
    ) -> Reply:
        """Send one customer message. Reuse `session_id` for the whole conversation."""
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        return Reply.from_json(
            self._call("POST", "messages", self._message_body(message, session_id, end_user_id, end_user_attributes, workflow), headers)
        )

    def stream(
        self,
        message: str,
        *,
        session_id: str | None = None,
        end_user_id: str | None = None,
        end_user_attributes: dict[str, Any] | None = None,
        workflow: str | None = None,
    ) -> Iterator[tuple[str, dict[str, Any]]]:
        """Like `send`, but yields ("stage", {...}) progress events and finally ("done", {...})."""
        body = self._message_body(message, session_id, end_user_id, end_user_attributes, workflow)
        body["stream"] = True
        with self._http.stream(
            "POST", self._path("messages"), json=body, headers=self._headers
        ) as response:
            if response.status_code >= 400:
                response.read()
                raise self._error(response)
            event = "message"
            for line in response.iter_lines():
                if line.startswith("event:"):
                    event = line.removeprefix("event:").strip()
                elif line.startswith("data:"):
                    yield event, json.loads(line.removeprefix("data:").strip())

    def confirm(self, session_id: str, confirmation_id: str, approve: bool) -> Reply:
        """Answer a `needs_confirmation` reply (for Yes/No buttons)."""
        return Reply.from_json(
            self._call(
                "POST",
                "confirmations",
                {"session_id": session_id, "confirmation_id": confirmation_id, "decision": "confirm" if approve else "cancel"},
            )
        )

    def send_tool_result(
        self, session_id: str, call_id: str, result: Any = None, error: str | None = None
    ) -> Reply:
        """Return what your system did for an `action_required` reply. Pass `error` if it failed."""
        body: dict[str, Any] = {"session_id": session_id, "call_id": call_id}
        if error is not None:
            body["error"] = error
        else:
            body["result"] = result
        return Reply.from_json(self._call("POST", "tool-results", body))

    # ---- sessions and privacy ----------------------------------------------------------------

    def session(self, session_id: str) -> dict[str, Any]:
        return self._call("GET", f"sessions/{session_id}")

    def delete_session(self, session_id: str) -> None:
        self._call("DELETE", f"sessions/{session_id}")

    def delete_end_user(self, end_user_id: str) -> int:
        """Erase everything stored about one customer. Returns how many sessions were deleted."""
        return int(self._call("DELETE", f"end-users/{end_user_id}")["deleted_sessions"])

    # ---- internals ---------------------------------------------------------------------------

    @staticmethod
    def _message_body(
        message: str,
        session_id: str | None,
        end_user_id: str | None,
        attributes: dict[str, Any] | None,
        workflow: str | None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"message": message}
        if session_id:
            body["session_id"] = session_id
        if end_user_id:
            body["end_user"] = {"id": end_user_id, "attributes": attributes or {}}
        if workflow:
            body["workflow"] = workflow
        return body

    def _path(self, tail: str) -> str:
        return f"/v1/agents/{self.agent_id}/{tail}"

    def _call(self, method: str, tail: str, body: dict[str, Any] | None = None, headers: dict[str, str] | None = None):
        response = self._http.request(
            method, self._path(tail), json=body, headers={**self._headers, **(headers or {})}
        )
        if response.status_code >= 400:
            raise self._error(response)
        return None if response.status_code == 204 else response.json()

    @staticmethod
    def _error(response: httpx.Response) -> AgentPlatformError:
        try:
            error = response.json()["error"]
            code, message = error["code"], error["message"]
        except (ValueError, KeyError, TypeError):
            code, message = "http_error", response.text[:200]
        retry = response.headers.get("Retry-After")
        return AgentPlatformError(response.status_code, code, message, int(retry) if retry and retry.isdigit() else None)


def verify_webhook(secret: str, signature_header: str, body: bytes, *, tolerance: int = 300) -> bool:
    """Check an incoming webhook: the HMAC matches AND the timestamp is recent (stops replays).

    `signature_header` is the `X-Webhook-Signature` header; `body` is the raw request body bytes.
    """
    try:
        parts = dict(item.split("=", 1) for item in signature_header.split(","))
        timestamp, supplied = int(parts["t"]), parts["v1"]
    except (KeyError, ValueError):
        return False
    if abs(time.time() - timestamp) > tolerance:
        return False
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, supplied)
