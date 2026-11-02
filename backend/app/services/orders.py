from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Order


def get_order(db: Session, *, tenant_id: UUID, order_number: str) -> Order | None:
    stmt = select(Order).where(
        Order.tenant_id == tenant_id,
        Order.order_number == order_number.upper(),
    )
    return db.scalars(stmt).first()
