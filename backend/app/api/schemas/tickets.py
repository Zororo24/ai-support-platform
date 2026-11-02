from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.db.models import TicketPriority, TicketStatus, UserRole


class TicketCreate(BaseModel):
    subject: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=10_000)
    priority: TicketPriority = TicketPriority.normal


class TicketPatch(BaseModel):
    """Admin triage. `assignee_id` is only applied when present in the request (null unassigns)."""

    status: TicketStatus | None = None
    priority: TicketPriority | None = None
    assignee_id: UUID | None = None


class TicketOut(BaseModel):
    id: UUID
    subject: str
    body: str
    status: TicketStatus
    priority: TicketPriority = TicketPriority.normal
    requester_email: str | None = None
    assignee_id: UUID | None = None
    assignee_email: str | None = None
    conversation_id: UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class CommentIn(BaseModel):
    body: str = Field(min_length=1, max_length=5_000)


class CommentOut(BaseModel):
    id: UUID
    author_email: str
    author_role: UserRole
    body: str
    created_at: datetime


class TicketDetail(TicketOut):
    comments: list[CommentOut]


class TicketList(BaseModel):
    items: list[TicketOut]
    total: int


class BulkStatusIn(BaseModel):
    ids: list[UUID] = Field(min_length=1, max_length=100)
    status: TicketStatus


class BulkStatusOut(BaseModel):
    updated: int
