from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.db.models import User, UserRole


class UserRuleViolation(Exception):
    """A change the system refuses (message is safe to show to the admin)."""


def list_users(db: Session, *, tenant_id: UUID) -> list[User]:
    return list(db.scalars(select(User).where(User.tenant_id == tenant_id).order_by(User.created_at)))


def get_user(db: Session, *, tenant_id: UUID, user_id: UUID) -> User | None:
    user = db.get(User, user_id)
    return user if user is not None and user.tenant_id == tenant_id else None


def create_user(db: Session, *, tenant_id: UUID, email: str, password: str, role: UserRole) -> User:
    email = email.strip().lower()
    # Login identifies people by email alone, so an address may exist in only one tenant.
    if db.scalars(select(User).where(func.lower(User.email) == email)).first() is not None:
        raise UserRuleViolation("That email address is already in use")
    user = User(
        tenant_id=tenant_id, email=email, password_hash=hash_password(password), role=role, is_active=True
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _other_active_admins(db: Session, target: User) -> int:
    return (
        db.scalar(
            select(func.count())
            .select_from(User)
            .where(
                User.tenant_id == target.tenant_id,
                User.role == UserRole.admin,
                User.is_active.is_(True),
                User.id != target.id,
            )
        )
        or 0
    )


def update_user(
    db: Session,
    *,
    actor: User,
    target: User,
    role: UserRole | None,
    is_active: bool | None,
    password: str | None,
) -> User:
    changes_role = role is not None and role != target.role
    changes_active = is_active is not None and is_active != target.is_active
    if target.id == actor.id and (changes_role or changes_active):
        raise UserRuleViolation("You can't change your own role or deactivate yourself")
    loses_admin = (
        target.role == UserRole.admin
        and target.is_active
        and ((changes_role and role != UserRole.admin) or (changes_active and is_active is False))
    )
    if loses_admin and _other_active_admins(db, target) == 0:
        raise UserRuleViolation("This is the last active admin; promote someone else first")

    if changes_role:
        target.role = role
    if changes_active:
        target.is_active = is_active
    if password is not None:
        target.password_hash = hash_password(password)
        target.token_version += 1  # a reset signs the person out everywhere
    db.commit()
    db.refresh(target)
    return target
