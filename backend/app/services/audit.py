"""Append-only audit trail for admin-relevant changes.

`record` commits on its own, after the audited action has succeeded, so a failed action
leaves no entry. Never put secrets (passwords, document bodies) in `detail`.
"""

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import AuditLog, User

logger = logging.getLogger(__name__)


def record(
    db: Session,
    *,
    actor: User,
    action: str,
    target_type: str,
    target_id: UUID | str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    try:
        db.add(
            AuditLog(
                tenant_id=actor.tenant_id,
                actor_id=actor.id,
                actor_email=actor.email,
                action=action,
                target_type=target_type,
                target_id=str(target_id) if target_id is not None else None,
                detail=detail,
            )
        )
        db.commit()
    except Exception:
        # Auditing must not turn a successful admin action into an error response.
        db.rollback()
        logger.exception("failed to write audit entry %s", action)


def list_entries(
    db: Session,
    *,
    tenant_id: UUID,
    action_prefix: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[AuditLog], int]:
    clauses = [AuditLog.tenant_id == tenant_id]
    if action_prefix:
        clauses.append(AuditLog.action.like(f"{action_prefix}%"))
    total = db.scalar(select(func.count()).select_from(AuditLog).where(*clauses)) or 0
    rows = db.scalars(
        select(AuditLog).where(*clauses).order_by(AuditLog.created_at.desc()).limit(limit).offset(offset)
    )
    return list(rows), total
