from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from app.api.schemas.orders import OrderOut
from app.api.schemas.tickets import TicketOut


class ChatMessageIn(BaseModel):
    content: str = Field(min_length=1)
    conversation_id: UUID | None = None


class CitationOut(BaseModel):
    document_id: UUID
    title: str
    snippet: str


class ChatMessageOut(BaseModel):
    conversation_id: UUID
    message_id: UUID | None = None
    content: str
    citations: list[CitationOut] = []
    ticket: TicketOut | None = None
    order: OrderOut | None = None
    intent: str | None = None
    cache_hit: bool = False
    meta: dict[str, Any] = {}
