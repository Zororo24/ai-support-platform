"""A fake company system for demos and tests.

This stands in for what a real customer would expose: an orders endpoint, a helpdesk, a returns
service, each authenticated by an API key and scoped to the caller's own customers. Seeded agents
call it over HTTP through an ordinary Connection, so the demo exercises exactly the code path a
real company's API would. Mounted only when ENABLE_DEMO_COMPANY_API is true.
"""

import secrets
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import DemoReturn, DemoTicket, Order, Tenant
from app.db.session import get_db

router = APIRouter(prefix="/demo-company", tags=["demo company (fake external system)"])


def demo_key(slug: str) -> str:
    return f"demo-{slug}-key"


def company(authorization: str | None = Header(default=None), db: Session = Depends(get_db)) -> Tenant:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() == "bearer" and token.startswith("demo-") and token.endswith("-key"):
        slug = token[len("demo-") : -len("-key")]
        tenant = db.scalars(select(Tenant).where(Tenant.slug == slug)).first()
        if tenant is not None and secrets.compare_digest(token, demo_key(tenant.slug)):
            return tenant
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid demo company key")


def _find_order(db: Session, tenant: Tenant, order_number: str, customer_id: str | None) -> Order:
    order = db.scalars(
        select(Order).where(Order.tenant_id == tenant.id, Order.order_number == order_number.upper())
    ).first()
    # A real system only shows a customer their own orders; unknown and foreign look identical.
    if order is None or (order.customer_ref and order.customer_ref != customer_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="order not found")
    return order


def _order_json(order: Order) -> dict[str, Any]:
    days = None
    if order.delivered_at is not None:
        days = (datetime.now(timezone.utc) - order.delivered_at).days
    return {
        "order_number": order.order_number,
        "status": order.status,
        "total_cents": order.total_cents,
        "item_name": order.item_name,
        "delivered_at": order.delivered_at.isoformat() if order.delivered_at else None,
        "days_since_delivery": days,
    }


@router.get("/orders/{order_number}")
def get_order(
    order_number: str,
    customer_id: str | None = None,
    tenant: Tenant = Depends(company),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return _order_json(_find_order(db, tenant, order_number, customer_id))


class TicketIn(BaseModel):
    subject: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=5000)
    customer_id: str = Field(min_length=1, max_length=200)


@router.post("/tickets", status_code=status.HTTP_201_CREATED)
def create_ticket(
    body: TicketIn, tenant: Tenant = Depends(company), db: Session = Depends(get_db)
) -> dict[str, Any]:
    ticket = DemoTicket(
        tenant_id=tenant.id, customer_ref=body.customer_id, subject=body.subject, body=body.body
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    return {"ticket_id": f"TCK-{str(ticket.id)[:8]}", "status": ticket.status}


@router.get("/tickets")
def list_tickets(
    customer_id: str, tenant: Tenant = Depends(company), db: Session = Depends(get_db)
) -> dict[str, Any]:
    rows = db.scalars(
        select(DemoTicket)
        .where(DemoTicket.tenant_id == tenant.id, DemoTicket.customer_ref == customer_id)
        .order_by(DemoTicket.created_at.desc())
    )
    return {"tickets": [{"subject": t.subject, "body": t.body, "status": t.status} for t in rows]}


class ReturnIn(BaseModel):
    order_number: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=500)
    customer_id: str = Field(min_length=1, max_length=200)


@router.post("/returns", status_code=status.HTTP_201_CREATED)
def create_return(
    body: ReturnIn, tenant: Tenant = Depends(company), db: Session = Depends(get_db)
) -> dict[str, Any]:
    order = _find_order(db, tenant, body.order_number, body.customer_id)
    if order.delivered_at is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="order has not been delivered yet")
    record = DemoReturn(
        tenant_id=tenant.id,
        order_number=order.order_number,
        customer_ref=body.customer_id,
        reason=body.reason,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return {
        "return_id": f"RET-{str(record.id)[:8]}",
        "label_url": f"https://returns.demo.test/labels/{record.id}",
        "status": record.status,
    }


@router.get("/returns")
def list_returns(
    customer_id: str, tenant: Tenant = Depends(company), db: Session = Depends(get_db)
) -> dict[str, Any]:
    rows = db.scalars(
        select(DemoReturn).where(DemoReturn.tenant_id == tenant.id, DemoReturn.customer_ref == customer_id)
    )
    return {
        "returns": [{"order_number": r.order_number, "reason": r.reason, "status": r.status} for r in rows]
    }
