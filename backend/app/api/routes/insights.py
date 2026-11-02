from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import require_roles
from app.api.schemas.insights import AuditEntryOut, AuditList, GapOut, SummaryOut
from app.db.models import User, UserRole
from app.db.session import get_db
from app.services import audit as audit_service
from app.services import insights as insights_service

router = APIRouter(prefix="/admin", tags=["admin"])

admin_only = require_roles(UserRole.admin)


@router.get("/insights/summary", response_model=SummaryOut)
def summary(
    days: int = Query(default=30, ge=1, le=90),
    agent_id: UUID | None = None,
    user: User = Depends(admin_only),
    db: Session = Depends(get_db),
) -> dict:
    return insights_service.summary(db, tenant_id=user.tenant_id, days=days, agent_id=agent_id)


@router.get("/insights/gaps", response_model=list[GapOut])
def gaps(
    days: int = Query(default=30, ge=1, le=90),
    agent_id: UUID | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    user: User = Depends(admin_only),
    db: Session = Depends(get_db),
) -> list[dict]:
    return insights_service.knowledge_gaps(
        db, tenant_id=user.tenant_id, days=days, limit=limit, agent_id=agent_id
    )


@router.get("/audit", response_model=AuditList)
def audit_log(
    action: str | None = Query(default=None, max_length=64, description="action prefix, e.g. 'document.'"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(admin_only),
    db: Session = Depends(get_db),
) -> AuditList:
    entries, total = audit_service.list_entries(
        db, tenant_id=user.tenant_id, action_prefix=action, limit=limit, offset=offset
    )
    return AuditList(
        items=[
            AuditEntryOut(
                id=e.id,
                actor_email=e.actor_email,
                action=e.action,
                target_type=e.target_type,
                target_id=e.target_id,
                detail=e.detail,
                created_at=e.created_at,
            )
            for e in entries
        ],
        total=total,
    )
