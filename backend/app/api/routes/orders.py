from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.api.schemas.orders import OrderOut
from app.db.models import User
from app.db.session import get_db
from app.services import orders as orders_service

router = APIRouter(prefix="/orders", tags=["orders"])


@router.get("/{order_number}", response_model=OrderOut)
def get_order(
    order_number: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> OrderOut:
    order = orders_service.get_order(db, tenant_id=user.tenant_id, order_number=order_number)
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    return OrderOut.model_validate(order, from_attributes=True)
