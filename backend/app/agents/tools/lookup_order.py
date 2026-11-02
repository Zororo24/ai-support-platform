from uuid import UUID

from sqlalchemy.orm import Session

from app.db.models import Order
from app.services import orders as orders_service


def lookup_order(db: Session, *, tenant_id: UUID, order_number: str) -> Order | None:
    """Same service the REST route uses, so the tenant filter cannot be bypassed here."""
    return orders_service.get_order(db, tenant_id=tenant_id, order_number=order_number)
