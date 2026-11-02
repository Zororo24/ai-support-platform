"""Signed outbound webhooks: how a company's own systems hear about handoffs, approvals and failures.

Delivery is at-least-once: each event is stored first, then delivered by the job queue with backoff
for about a day, so a slow or down endpoint loses nothing. Receivers de-duplicate on the event id
and verify the signature (see `verify_signature`).
"""

import hashlib
import hmac
import json
import logging
import secrets as pysecrets
import time
import uuid
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core import safe_http, ssrf
from app.core import secrets as vault
from app.db.models import Agent, WebhookDelivery, WebhookEndpoint
from app.services import jobs

logger = logging.getLogger(__name__)

EVENT_TYPES = (
    "run.completed",
    "run.failed",
    "handoff.requested",
    "approval.requested",
    "approval.decided",
    "action.failed",
    "budget.warning",
    "budget.exceeded",
)
MAX_ATTEMPTS = 8
TIMEOUT_SECONDS = 10.0
SIGNATURE_TOLERANCE_SECONDS = 300


class WebhookRuleError(Exception):
    """A refused webhook change; the message is safe to show the admin."""


# ---- signing -----------------------------------------------------------------------------------


def sign(secret: str, timestamp: int, body: bytes) -> str:
    digest = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def verify_signature(
    secret: str,
    header: str,
    body: bytes,
    *,
    tolerance: int = SIGNATURE_TOLERANCE_SECONDS,
    now: float | None = None,
) -> bool:
    """What a receiver should do: check the HMAC and reject stale timestamps (replay protection)."""
    try:
        parts = dict(item.split("=", 1) for item in header.split(","))
        timestamp = int(parts["t"])
        supplied = parts["v1"]
    except (KeyError, ValueError):
        return False
    if abs((now if now is not None else time.time()) - timestamp) > tolerance:
        return False
    expected = sign(secret, timestamp, body).split("v1=", 1)[1]
    return hmac.compare_digest(expected, supplied)


# ---- endpoints ---------------------------------------------------------------------------------


def get_endpoint(db: Session, *, tenant_id: UUID, endpoint_id: UUID) -> WebhookEndpoint | None:
    endpoint = db.get(WebhookEndpoint, endpoint_id)
    return endpoint if endpoint is not None and endpoint.tenant_id == tenant_id else None


def list_endpoints(db: Session, *, tenant_id: UUID) -> list[WebhookEndpoint]:
    return list(
        db.scalars(
            select(WebhookEndpoint)
            .where(WebhookEndpoint.tenant_id == tenant_id)
            .order_by(WebhookEndpoint.created_at)
        )
    )


def _check(url: str, events: list[str]) -> list[str]:
    try:
        ssrf.validate_url(url)
    except ssrf.BlockedDestination as exc:
        raise WebhookRuleError(f"url: {exc}") from exc
    cleaned = list(dict.fromkeys(events))
    if not cleaned:
        raise WebhookRuleError("events: choose at least one event")
    unknown = [e for e in cleaned if e != "*" and e not in EVENT_TYPES]
    if unknown:
        raise WebhookRuleError(f"events: unknown event '{unknown[0]}'")
    return cleaned


def _new_secret() -> str:
    return "whsec_" + pysecrets.token_urlsafe(24)


def _check_agent(db: Session, tenant_id: UUID, agent_id: UUID | None) -> None:
    if agent_id is not None:
        agent = db.get(Agent, agent_id)
        if agent is None or agent.tenant_id != tenant_id:
            raise WebhookRuleError("agent_id: unknown agent")


def create_endpoint(
    db: Session,
    *,
    tenant_id: UUID,
    user_id: UUID | None,
    url: str,
    events: list[str],
    agent_id: UUID | None,
    description: str,
) -> tuple[WebhookEndpoint, str]:
    events = _check(url, events)
    _check_agent(db, tenant_id, agent_id)
    secret = _new_secret()
    endpoint = WebhookEndpoint(
        tenant_id=tenant_id,
        agent_id=agent_id,
        url=url.strip(),
        description=description.strip(),
        secret_encrypted=vault.encrypt(secret),
        secret_hint=vault.hint(secret),
        events=events,
        created_by=user_id,
    )
    db.add(endpoint)
    db.commit()
    db.refresh(endpoint)
    return endpoint, secret


def update_endpoint(
    db: Session,
    *,
    endpoint: WebhookEndpoint,
    url: str | None,
    events: list[str] | None,
    active: bool | None,
    description: str | None,
    rotate_secret: bool,
) -> str | None:
    """Apply changes. Returns the new signing secret only when it was rotated."""
    if url is not None or events is not None:
        events = _check(url or endpoint.url, events if events is not None else list(endpoint.events))
    if url is not None:
        endpoint.url = url.strip()
    if events is not None:
        endpoint.events = events
    if active is not None:
        endpoint.active = active
    if description is not None:
        endpoint.description = description.strip()
    new_secret = None
    if rotate_secret:
        new_secret = _new_secret()
        endpoint.secret_encrypted, endpoint.secret_hint = vault.encrypt(new_secret), vault.hint(new_secret)
    db.commit()
    return new_secret


def delete_endpoint(db: Session, *, endpoint: WebhookEndpoint) -> None:
    db.delete(endpoint)
    db.commit()


# ---- emitting and delivering ---------------------------------------------------------------------


def _envelope(event_id: str, event_type: str, agent_id: UUID | None, data: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": event_id,
        "type": event_type,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "agent_id": str(agent_id) if agent_id else None,
        "data": data,
    }


def _queue_delivery(db: Session, endpoint: WebhookEndpoint, envelope: dict[str, Any]) -> WebhookDelivery:
    delivery = WebhookDelivery(
        tenant_id=endpoint.tenant_id,
        endpoint_id=endpoint.id,
        event_id=envelope["id"],
        event_type=envelope["type"],
        payload=envelope,
    )
    db.add(delivery)
    db.commit()
    db.refresh(delivery)
    job = jobs.enqueue(
        db,
        type="webhook.deliver",
        payload={"delivery_id": str(delivery.id)},
        tenant_id=endpoint.tenant_id,
        max_attempts=MAX_ATTEMPTS,
    )
    jobs.kick(job.id)
    return delivery


def emit(
    db: Session, *, tenant_id: UUID, agent_id: UUID | None, event_type: str, data: dict[str, Any]
) -> int:
    """Queue `event_type` for every active endpoint that wants it. Returns how many deliveries."""
    endpoints = db.scalars(
        select(WebhookEndpoint).where(
            WebhookEndpoint.tenant_id == tenant_id,
            WebhookEndpoint.active.is_(True),
            or_(WebhookEndpoint.agent_id.is_(None), WebhookEndpoint.agent_id == agent_id),
        )
    ).all()
    wanted = [e for e in endpoints if "*" in e.events or event_type in e.events]
    if not wanted:
        return 0
    envelope = _envelope("evt_" + uuid.uuid4().hex[:24], event_type, agent_id, data)
    for endpoint in wanted:
        _queue_delivery(db, endpoint, envelope)
    return len(wanted)


def send_test(db: Session, *, endpoint: WebhookEndpoint) -> WebhookDelivery:
    envelope = _envelope(
        "evt_" + uuid.uuid4().hex[:24], "ping", endpoint.agent_id, {"message": "This is a test event."}
    )
    return _queue_delivery(db, endpoint, envelope)


def replay(db: Session, *, delivery: WebhookDelivery) -> WebhookDelivery | None:
    endpoint = db.get(WebhookEndpoint, delivery.endpoint_id)
    if endpoint is None:
        return None
    return _queue_delivery(db, endpoint, delivery.payload)  # same event id: receivers can de-duplicate


@jobs.handler("webhook.deliver")
def deliver(db: Session, payload: dict[str, Any]) -> None:
    delivery = db.get(WebhookDelivery, UUID(payload["delivery_id"]))
    if delivery is None or delivery.status == "delivered":
        return
    endpoint = db.get(WebhookEndpoint, delivery.endpoint_id)
    if endpoint is None:
        delivery.status, delivery.last_error = "failed", "the endpoint was deleted"
        db.commit()
        return
    if not endpoint.active and delivery.event_type != "ping":
        delivery.status, delivery.last_error = "failed", "the endpoint is disabled"
        db.commit()
        return

    body = json.dumps(delivery.payload, separators=(",", ":"), sort_keys=True).encode()
    timestamp = int(time.time())
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "AgentPlatform-Webhooks/1.0",
        "X-Webhook-Id": delivery.event_id,
        "X-Webhook-Event": delivery.event_type,
        "X-Webhook-Timestamp": str(timestamp),
        "X-Webhook-Signature": sign(vault.decrypt(endpoint.secret_encrypted), timestamp, body),
    }
    sent = safe_http.post(endpoint.url, content=body, headers=headers, timeout=TIMEOUT_SECONDS)
    delivery.attempts += 1
    delivery.response_code = sent.status_code
    if sent.ok:
        delivery.status, delivery.last_error = "delivered", None
        delivery.delivered_at = datetime.now(timezone.utc)
        db.commit()
        return
    reason = sent.error or f"the endpoint answered with status {sent.status_code}"
    delivery.last_error = reason[:500]
    if delivery.attempts >= MAX_ATTEMPTS:
        delivery.status = "failed"
        db.commit()
        logger.warning("webhook %s gave up after %d attempts: %s", delivery.id, delivery.attempts, reason)
        return
    db.commit()
    raise jobs.RetryLater(reason)


# ---- reading the log -----------------------------------------------------------------------------


def list_deliveries(
    db: Session,
    *,
    tenant_id: UUID,
    endpoint_id: UUID | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[WebhookDelivery], int]:
    from sqlalchemy import func

    conditions = [WebhookDelivery.tenant_id == tenant_id]
    if endpoint_id is not None:
        conditions.append(WebhookDelivery.endpoint_id == endpoint_id)
    if status:
        conditions.append(WebhookDelivery.status == status)
    total = db.scalar(select(func.count()).select_from(WebhookDelivery).where(*conditions)) or 0
    rows = db.scalars(
        select(WebhookDelivery)
        .where(*conditions)
        .order_by(WebhookDelivery.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(rows), total
