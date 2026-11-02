from uuid import UUID

from pydantic import BaseModel


class OrderOut(BaseModel):
    id: UUID
    order_number: str
    status: str
    total_cents: int
