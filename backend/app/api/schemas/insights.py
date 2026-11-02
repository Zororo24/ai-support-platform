from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel


class GapOut(BaseModel):
    question: str
    count: int
    refused: int
    thumbs_down: int
    last_seen: datetime


class DayPoint(BaseModel):
    date: str
    conversations: int
    messages: int


class SummaryOut(BaseModel):
    days: int
    conversations: int
    replies: int
    outcomes: dict[str, int]
    answered_rate: float | None
    tickets_from_chat: int
    thumbs_up: int
    thumbs_down: int
    helpful_rate: float | None
    avg_latency_ms: int | None
    total_tokens: int
    series: list[DayPoint]
    top_gaps: list[GapOut]


class AuditEntryOut(BaseModel):
    id: UUID
    actor_email: str
    action: str
    target_type: str
    target_id: str | None
    detail: dict[str, Any] | None
    created_at: datetime


class AuditList(BaseModel):
    items: list[AuditEntryOut]
    total: int
