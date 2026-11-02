from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.api.schemas.auth import LoginRequest, TokenResponse, UserOut
from app.core import ratelimit
from app.core.config import get_settings
from app.core.security import create_access_token, verify_password
from app.db.models import Tenant, User
from app.db.session import get_db

router = APIRouter(prefix="/auth", tags=["auth"])


def user_out(db: Session, user: User) -> UserOut:
    tenant = db.get(Tenant, user.tenant_id)
    return UserOut(
        id=user.id,
        email=user.email,
        role=user.role,
        tenant_id=user.tenant_id,
        tenant_slug=tenant.slug,
        tenant_name=tenant.name,
    )


def _issue_token(user: User) -> str:
    return create_access_token(
        user_id=user.id,
        tenant_id=user.tenant_id,
        role=user.role.value,
        token_version=user.token_version,
    )


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, request: Request, db: Session = Depends(get_db)) -> TokenResponse:
    # Pre-auth, so there is no tenant to key on: throttle by address and by client IP (5 min windows).
    settings = get_settings()
    email = body.email.lower()
    ip = request.client.host if request.client else "unknown"
    ratelimit.raise_if_limited(
        ratelimit.hit(f"login:email:{email}", limit=settings.login_attempts_per_email, window=300)
    )
    ratelimit.raise_if_limited(
        ratelimit.hit(f"login:ip:{ip}", limit=settings.login_attempts_per_ip, window=300)
    )
    # Emails are unique per tenant, not globally; login is only unambiguous while
    # an address belongs to one tenant, so reject the ambiguous case outright.
    users = list(db.scalars(select(User).where(User.email == body.email.lower())))
    matches = [u for u in users if u.is_active and verify_password(body.password, u.password_hash)]
    if len(matches) != 1:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    user = matches[0]
    token = _issue_token(user)
    return TokenResponse(access_token=token, user=user_out(db, user))


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> UserOut:
    return user_out(db, user)


@router.post("/refresh", response_model=TokenResponse)
def refresh(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> TokenResponse:
    """Sliding session: trade a still-valid token for a fresh one with a full lifetime.

    An expired or revoked token cannot be refreshed; the person has to sign in again.
    """
    return TokenResponse(access_token=_issue_token(user), user=user_out(db, user))


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT)
def logout_all(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> None:
    """Revoke every token issued to this user, on every device (including the one calling)."""
    user.token_version += 1
    db.commit()
