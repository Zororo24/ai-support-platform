"""Workspace settings (dashboard): the workspace's own AI provider key."""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import require_roles
from app.db.models import User, UserRole
from app.db.session import get_db
from app.services import audit, tenant_llm

router = APIRouter(prefix="/admin/settings", tags=["settings"])

admin_only = require_roles(UserRole.admin)


class LlmKeyIn(BaseModel):
    api_key: str = Field(min_length=8, max_length=500)


class LlmKeyOut(BaseModel):
    configured: bool
    hint: str | None = None  # the last characters only; the key itself can never be read back


@router.get("/llm-key", response_model=LlmKeyOut)
def get_llm_key(user: User = Depends(admin_only), db: Session = Depends(get_db)) -> LlmKeyOut:
    return LlmKeyOut(**tenant_llm.status(db, user.tenant_id))


@router.put("/llm-key", response_model=LlmKeyOut)
def set_llm_key(body: LlmKeyIn, user: User = Depends(admin_only), db: Session = Depends(get_db)) -> LlmKeyOut:
    """Use your own AI provider key for this workspace. It is checked with the provider first."""
    try:
        tenant_llm.set_key(db, tenant_id=user.tenant_id, api_key=body.api_key)
    except tenant_llm.KeyRejected as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    audit.record(
        db, actor=user, action="settings.llm_key_set", target_type="workspace", target_id=user.tenant_id
    )
    return LlmKeyOut(**tenant_llm.status(db, user.tenant_id))


@router.delete("/llm-key", response_model=LlmKeyOut)
def clear_llm_key(user: User = Depends(admin_only), db: Session = Depends(get_db)) -> LlmKeyOut:
    """Go back to the platform's shared key."""
    tenant_llm.clear_key(db, tenant_id=user.tenant_id)
    audit.record(
        db, actor=user, action="settings.llm_key_cleared", target_type="workspace", target_id=user.tenant_id
    )
    return LlmKeyOut(**tenant_llm.status(db, user.tenant_id))
