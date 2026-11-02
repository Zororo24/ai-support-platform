from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import require_roles
from app.db.models import User, UserRole, WebhookDelivery, WebhookEndpoint
from app.db.session import get_db
from app.services import audit
from app.services import webhooks as svc

router = APIRouter(prefix="/admin/webhooks", tags=["webhooks"])

admin_only = require_roles(UserRole.admin)


class EndpointIn(BaseModel):
    url: str = Field(min_length=8, max_length=500)
    events: list[str] = Field(default_factory=lambda: ["*"], max_length=20)
    agent_id: UUID | None = None  # none = every agent in the workspace
    description: str = Field(default="", max_length=200)


class EndpointPatch(BaseModel):
    url: str | None = Field(default=None, min_length=8, max_length=500)
    events: list[str] | None = Field(default=None, max_length=20)
    active: bool | None = None
    description: str | None = Field(default=None, max_length=200)
    rotate_secret: bool = False


class EndpointOut(BaseModel):
    id: UUID
    url: str
    description: str
    agent_id: UUID | None
    events: list[str]
    active: bool
    secret_hint: str
    created_at: datetime
    # Only present in the response that created or rotated the secret; it cannot be read again.
    secret: str | None = None


class DeliveryOut(BaseModel):
    id: UUID
    event_id: str
    event_type: str
    status: str
    attempts: int
    response_code: int | None
    last_error: str | None
    created_at: datetime
    delivered_at: datetime | None
    payload: dict


def _out(endpoint: WebhookEndpoint, secret: str | None = None) -> EndpointOut:
    return EndpointOut(
        id=endpoint.id,
        url=endpoint.url,
        description=endpoint.description,
        agent_id=endpoint.agent_id,
        events=list(endpoint.events),
        active=endpoint.active,
        secret_hint=endpoint.secret_hint,
        created_at=endpoint.created_at,
        secret=secret,
    )


def _delivery_out(d: WebhookDelivery) -> DeliveryOut:
    return DeliveryOut(
        id=d.id,
        event_id=d.event_id,
        event_type=d.event_type,
        status=d.status,
        attempts=d.attempts,
        response_code=d.response_code,
        last_error=d.last_error,
        created_at=d.created_at,
        delivered_at=d.delivered_at,
        payload=d.payload,
    )


def _require(db: Session, user: User, endpoint_id: UUID) -> WebhookEndpoint:
    endpoint = svc.get_endpoint(db, tenant_id=user.tenant_id, endpoint_id=endpoint_id)
    if endpoint is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Webhook not found")
    return endpoint


def _rule(exc: svc.WebhookRuleError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


@router.get("/events", response_model=list[str])
def event_types(_: User = Depends(admin_only)) -> list[str]:
    return list(svc.EVENT_TYPES)


@router.get("", response_model=list[EndpointOut])
def list_webhooks(user: User = Depends(admin_only), db: Session = Depends(get_db)) -> list[EndpointOut]:
    return [_out(e) for e in svc.list_endpoints(db, tenant_id=user.tenant_id)]


@router.post("", response_model=EndpointOut, status_code=status.HTTP_201_CREATED)
def create_webhook(
    body: EndpointIn, user: User = Depends(admin_only), db: Session = Depends(get_db)
) -> EndpointOut:
    try:
        endpoint, secret = svc.create_endpoint(
            db,
            tenant_id=user.tenant_id,
            user_id=user.id,
            url=body.url,
            events=body.events,
            agent_id=body.agent_id,
            description=body.description,
        )
    except svc.WebhookRuleError as exc:
        raise _rule(exc)
    audit.record(
        db, actor=user, action="webhook.create", target_type="webhook", target_id=endpoint.id,
        detail={"url": endpoint.url, "events": endpoint.events},
    )  # fmt: skip
    return _out(endpoint, secret)


@router.patch("/{endpoint_id}", response_model=EndpointOut)
def patch_webhook(
    endpoint_id: UUID,
    body: EndpointPatch,
    user: User = Depends(admin_only),
    db: Session = Depends(get_db),
) -> EndpointOut:
    endpoint = _require(db, user, endpoint_id)
    try:
        secret = svc.update_endpoint(
            db,
            endpoint=endpoint,
            url=body.url,
            events=body.events,
            active=body.active,
            description=body.description,
            rotate_secret=body.rotate_secret,
        )
    except svc.WebhookRuleError as exc:
        raise _rule(exc)
    audit.record(
        db, actor=user, action="webhook.update", target_type="webhook", target_id=endpoint.id,
        detail={"url": endpoint.url, "active": endpoint.active, "secret_rotated": secret is not None},
    )  # fmt: skip
    return _out(endpoint, secret)


@router.delete("/{endpoint_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_webhook(
    endpoint_id: UUID, user: User = Depends(admin_only), db: Session = Depends(get_db)
) -> None:
    endpoint = _require(db, user, endpoint_id)
    url = endpoint.url
    svc.delete_endpoint(db, endpoint=endpoint)
    audit.record(
        db, actor=user, action="webhook.delete", target_type="webhook", target_id=endpoint_id,
        detail={"url": url},
    )  # fmt: skip


@router.post("/{endpoint_id}/test", response_model=DeliveryOut)
def test_webhook(
    endpoint_id: UUID, user: User = Depends(admin_only), db: Session = Depends(get_db)
) -> DeliveryOut:
    """Send a `ping` event now so you can check your receiver and signature handling."""
    endpoint = _require(db, user, endpoint_id)
    delivery = svc.send_test(db, endpoint=endpoint)
    db.refresh(delivery)
    return _delivery_out(delivery)


@router.get("/{endpoint_id}/deliveries", response_model=list[DeliveryOut])
def deliveries(
    endpoint_id: UUID,
    limit: int = Query(default=30, ge=1, le=100),
    user: User = Depends(admin_only),
    db: Session = Depends(get_db),
) -> list[DeliveryOut]:
    endpoint = _require(db, user, endpoint_id)
    rows = db.scalars(
        select(WebhookDelivery)
        .where(WebhookDelivery.endpoint_id == endpoint.id, WebhookDelivery.tenant_id == user.tenant_id)
        .order_by(WebhookDelivery.created_at.desc())
        .limit(limit)
    )
    return [_delivery_out(d) for d in rows]


@router.post("/{endpoint_id}/deliveries/{delivery_id}/replay", response_model=DeliveryOut)
def replay(
    endpoint_id: UUID,
    delivery_id: UUID,
    user: User = Depends(admin_only),
    db: Session = Depends(get_db),
) -> DeliveryOut:
    """Send the same event again (same event id, so a receiver can recognise a repeat)."""
    endpoint = _require(db, user, endpoint_id)
    delivery = db.get(WebhookDelivery, delivery_id)
    if delivery is None or delivery.endpoint_id != endpoint.id or delivery.tenant_id != user.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Delivery not found")
    again = svc.replay(db, delivery=delivery)
    if again is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Webhook not found")
    db.refresh(again)
    return _delivery_out(again)
