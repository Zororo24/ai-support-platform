from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import require_roles
from app.core.llm import LLMUnavailable
from app.db.models import User, UserRole
from app.db.session import get_db
from app.services import approvals as svc
from app.services import audit

router = APIRouter(prefix="/admin/approvals", tags=["approvals"])

admin_only = require_roles(UserRole.admin)


class ApprovalOut(BaseModel):
    id: UUID
    agent_id: UUID
    agent_name: str
    run_id: UUID
    end_user_ref: str
    session_id: str
    action: str
    summary: str
    args: Any
    status: str
    requested_at: datetime
    expires_at: datetime
    decided_by: str | None
    decided_at: datetime | None
    note: str


class ApprovalList(BaseModel):
    items: list[ApprovalOut]
    total: int


class DecisionIn(BaseModel):
    decision: Literal["approve", "deny"]
    note: str = Field(default="", max_length=300)


class DecisionOut(BaseModel):
    status: str  # how the customer's request ended up: completed, failed, action_required, ...
    outcome: str
    reply: str


@router.get("", response_model=ApprovalList)
def list_approvals(
    status_filter: Literal["pending", "approved", "denied", "expired"] | None = Query(
        default=None, alias="status"
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(admin_only),
    db: Session = Depends(get_db),
) -> ApprovalList:
    items, total = svc.list_approvals(
        db, tenant_id=user.tenant_id, status=status_filter, limit=limit, offset=offset
    )
    return ApprovalList(items=[ApprovalOut(**{**i, "note": i["note"] or ""}) for i in items], total=total)


@router.post("/{approval_id}/decision", response_model=DecisionOut)
def decide(
    approval_id: UUID,
    body: DecisionIn,
    user: User = Depends(admin_only),
    db: Session = Depends(get_db),
) -> DecisionOut:
    """Allow or refuse the action. Allowing runs exactly what was shown, then the process carries on."""
    try:
        result = svc.decide(db, user=user, approval_id=approval_id, decision=body.decision, note=body.note)
    except svc.ApprovalError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
    except LLMUnavailable:
        raise HTTPException(status_code=503, detail="The model is unavailable; the decision was recorded.")
    audit.record(
        db, actor=user, action=f"approval.{body.decision}", target_type="approval", target_id=approval_id,
        detail={"note": body.note},
    )  # fmt: skip
    return DecisionOut(status=result["status"], outcome=result["outcome"], reply=result["reply"])
