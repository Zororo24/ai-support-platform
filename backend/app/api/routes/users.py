from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import require_roles
from app.api.schemas.users import UserAdminOut, UserCreate, UserPatch
from app.db.models import User, UserRole
from app.db.session import get_db
from app.services import audit
from app.services import users as users_service

router = APIRouter(prefix="/admin/users", tags=["admin"])

admin_only = require_roles(UserRole.admin)


def _out(user: User) -> UserAdminOut:
    return UserAdminOut(
        id=user.id, email=user.email, role=user.role, is_active=user.is_active, created_at=user.created_at
    )


@router.get("", response_model=list[UserAdminOut])
def list_users(user: User = Depends(admin_only), db: Session = Depends(get_db)) -> list[UserAdminOut]:
    return [_out(u) for u in users_service.list_users(db, tenant_id=user.tenant_id)]


@router.post("", response_model=UserAdminOut, status_code=status.HTTP_201_CREATED)
def create_user(
    body: UserCreate, user: User = Depends(admin_only), db: Session = Depends(get_db)
) -> UserAdminOut:
    try:
        created = users_service.create_user(
            db, tenant_id=user.tenant_id, email=body.email, password=body.password, role=body.role
        )
    except users_service.UserRuleViolation as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    audit.record(
        db,
        actor=user,
        action="user.create",
        target_type="user",
        target_id=created.id,
        detail={"email": created.email, "role": created.role.value},
    )
    return _out(created)


@router.patch("/{user_id}", response_model=UserAdminOut)
def patch_user(
    user_id: UUID,
    body: UserPatch,
    actor: User = Depends(admin_only),
    db: Session = Depends(get_db),
) -> UserAdminOut:
    target = users_service.get_user(db, tenant_id=actor.tenant_id, user_id=user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    before = {"role": target.role.value, "is_active": target.is_active}
    try:
        updated = users_service.update_user(
            db,
            actor=actor,
            target=target,
            role=body.role,
            is_active=body.is_active,
            password=body.password,
        )
    except users_service.UserRuleViolation as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    detail: dict = {"email": updated.email}
    if before["role"] != updated.role.value:
        detail["role"] = {"from": before["role"], "to": updated.role.value}
    if before["is_active"] != updated.is_active:
        detail["is_active"] = {"from": before["is_active"], "to": updated.is_active}
    if body.password is not None:
        detail["password_reset"] = True  # the value itself is never logged
    audit.record(
        db, actor=actor, action="user.update", target_type="user", target_id=updated.id, detail=detail
    )
    return _out(updated)
