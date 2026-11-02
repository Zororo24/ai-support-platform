"""Aggregate views over assistant replies for the admin dashboard.

Aggregation is done in Python over a column-only projection capped at MAX_ROWS recent replies,
which is simple and plenty for a single tenant; move to SQL GROUP BY if volumes grow.
"""

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Message, MessageRole, Ticket

MAX_ROWS = 50_000
ANSWERED = "answered"
REFUSED = "refused"


def _normalize(question: str) -> str:
    return " ".join(question.lower().split())


def _rows(db: Session, tenant_id: UUID, since: datetime, agent_id: UUID | None = None):
    stmt = (
        select(Message.conversation_id, Message.created_at, Message.feedback, Message.meta)
        .where(
            Message.tenant_id == tenant_id,
            Message.role == MessageRole.assistant,
            Message.created_at >= since,
        )
        .order_by(Message.created_at.desc())
        .limit(MAX_ROWS)
    )
    if agent_id is not None:
        # Only replies produced by this agent (old dashboard chats carry no agent id).
        stmt = stmt.where(Message.meta["agent_id"].astext == str(agent_id))
    return db.execute(stmt).all()


def knowledge_gaps(
    db: Session, *, tenant_id: UUID, days: int, limit: int = 50, agent_id: UUID | None = None
) -> list[dict[str, Any]]:
    """Questions the assistant could not answer from documents, or that customers rated poorly."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    groups: dict[str, dict[str, Any]] = {}
    for _conv, created_at, feedback, meta in _rows(db, tenant_id, since, agent_id):
        meta = meta or {}
        question = meta.get("question")
        if not question or meta.get("intent") != "knowledge":
            continue
        refused = meta.get("outcome") == REFUSED
        thumbs_down = feedback == -1
        if not (refused or thumbs_down):
            continue
        group = groups.setdefault(
            _normalize(question),
            {"question": question, "count": 0, "refused": 0, "thumbs_down": 0, "last_seen": created_at},
        )
        group["count"] += 1
        group["refused"] += int(refused)
        group["thumbs_down"] += int(thumbs_down)
        group["last_seen"] = max(group["last_seen"], created_at)
    ranked = sorted(groups.values(), key=lambda g: (g["count"], g["last_seen"]), reverse=True)
    return ranked[:limit]


def summary(db: Session, *, tenant_id: UUID, days: int, agent_id: UUID | None = None) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    rows = _rows(db, tenant_id, since, agent_id)

    outcomes: Counter[str] = Counter()
    conversations: set[UUID] = set()
    per_day: dict[str, dict[str, set | int]] = {}
    up = down = 0
    latencies: list[int] = []
    tokens = 0
    for conversation_id, created_at, feedback, meta in rows:
        meta = meta or {}
        outcomes[meta.get("outcome", "unknown")] += 1
        conversations.add(conversation_id)
        day = created_at.astimezone(timezone.utc).date().isoformat()
        bucket = per_day.setdefault(day, {"conversations": set(), "messages": 0})
        bucket["conversations"].add(conversation_id)  # type: ignore[union-attr]
        bucket["messages"] += 1  # type: ignore[operator]
        up += feedback == 1
        down += feedback == -1
        if isinstance(meta.get("latency_ms"), int):
            latencies.append(meta["latency_ms"])
        tokens += int(meta.get("total_tokens") or 0)

    # Continuous day axis so quiet days show as zero.
    series = []
    for offset in range(days - 1, -1, -1):
        day = (now - timedelta(days=offset)).date().isoformat()
        bucket = per_day.get(day)
        series.append(
            {
                "date": day,
                "conversations": len(bucket["conversations"]) if bucket else 0,  # type: ignore[arg-type]
                "messages": bucket["messages"] if bucket else 0,
            }
        )

    knowledge_total = outcomes[ANSWERED] + outcomes[REFUSED]
    tickets_from_chat = len(
        db.execute(
            select(Ticket.id).where(
                Ticket.tenant_id == tenant_id,
                Ticket.conversation_id.is_not(None),
                Ticket.created_at >= since,
            )
        ).all()
    )
    rated = up + down
    return {
        "days": days,
        "conversations": len(conversations),
        "replies": len(rows),
        "outcomes": dict(outcomes),
        "answered_rate": round(outcomes[ANSWERED] / knowledge_total, 3) if knowledge_total else None,
        "tickets_from_chat": tickets_from_chat,
        "thumbs_up": up,
        "thumbs_down": down,
        "helpful_rate": round(up / rated, 3) if rated else None,
        "avg_latency_ms": round(sum(latencies) / len(latencies)) if latencies else None,
        "total_tokens": tokens,
        "series": series,
        "top_gaps": knowledge_gaps(db, tenant_id=tenant_id, days=days, limit=5, agent_id=agent_id),
    }
