from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import require_roles
from app.db.models import Connection, User, UserRole
from app.db.session import get_db
from app.services import audit
from app.services import connections as svc

router = APIRouter(prefix="/admin/connections", tags=["connections"])

admin_only = require_roles(UserRole.admin)

AuthType = Literal["none", "bearer", "header", "basic"]


class ConnectionIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    base_url: str = Field(min_length=8, max_length=500)
    auth_type: AuthType = "none"
    auth_header_name: str | None = Field(default=None, max_length=60)
    secret: str | None = Field(default=None, min_length=1, max_length=2000)


class ConnectionPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    base_url: str | None = Field(default=None, min_length=8, max_length=500)
    auth_type: AuthType | None = None
    auth_header_name: str | None = Field(default=None, max_length=60)
    secret: str | None = Field(default=None, min_length=1, max_length=2000)  # replaces the stored secret


class ConnectionOut(BaseModel):
    id: UUID
    name: str
    base_url: str
    auth_type: str
    auth_header_name: str | None
    has_secret: bool
    secret_hint: str | None  # last four characters only; the secret itself is never returned
    used_by: list[str]
    created_at: datetime


def _out(db: Session, connection: Connection) -> ConnectionOut:
    return ConnectionOut(
        id=connection.id,
        name=connection.name,
        base_url=connection.base_url,
        auth_type=connection.auth_type,
        auth_header_name=connection.auth_header_name,
        has_secret=bool(connection.secret_encrypted),
        secret_hint=connection.secret_hint,
        used_by=svc.used_by(db, tenant_id=connection.tenant_id, connection_id=connection.id),
        created_at=connection.created_at,
    )


def _require(db: Session, user: User, connection_id: UUID) -> Connection:
    connection = svc.get_connection(db, tenant_id=user.tenant_id, connection_id=connection_id)
    if connection is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found")
    return connection


def _rule(exc: svc.ConnectionRuleError) -> HTTPException:
    code = (
        status.HTTP_409_CONFLICT
        if isinstance(exc, svc.ConnectionNameTaken)
        else status.HTTP_422_UNPROCESSABLE_ENTITY
    )
    return HTTPException(status_code=code, detail=str(exc))


@router.get("", response_model=list[ConnectionOut])
def list_connections(user: User = Depends(admin_only), db: Session = Depends(get_db)) -> list[ConnectionOut]:
    return [_out(db, c) for c in svc.list_connections(db, tenant_id=user.tenant_id)]


@router.post("", response_model=ConnectionOut, status_code=status.HTTP_201_CREATED)
def create_connection(
    body: ConnectionIn, user: User = Depends(admin_only), db: Session = Depends(get_db)
) -> ConnectionOut:
    try:
        connection = svc.create_connection(
            db,
            tenant_id=user.tenant_id,
            user_id=user.id,
            name=body.name,
            base_url=body.base_url,
            auth_type=body.auth_type,
            auth_header_name=body.auth_header_name,
            secret=body.secret,
        )
    except svc.ConnectionRuleError as exc:
        raise _rule(exc)
    audit.record(
        db, actor=user, action="connection.create", target_type="connection", target_id=connection.id,
        detail={"name": connection.name, "base_url": connection.base_url, "auth_type": connection.auth_type},
    )  # fmt: skip
    return _out(db, connection)


@router.patch("/{connection_id}", response_model=ConnectionOut)
def patch_connection(
    connection_id: UUID,
    body: ConnectionPatch,
    user: User = Depends(admin_only),
    db: Session = Depends(get_db),
) -> ConnectionOut:
    connection = _require(db, user, connection_id)
    try:
        svc.update_connection(
            db,
            connection=connection,
            name=body.name,
            base_url=body.base_url,
            auth_type=body.auth_type,
            auth_header_name=body.auth_header_name,
            secret=body.secret,
        )
    except svc.ConnectionRuleError as exc:
        raise _rule(exc)
    audit.record(
        db, actor=user, action="connection.update", target_type="connection", target_id=connection.id,
        detail={"name": connection.name, "secret_replaced": body.secret is not None},
    )  # fmt: skip
    return _out(db, connection)


@router.delete("/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_connection(
    connection_id: UUID, user: User = Depends(admin_only), db: Session = Depends(get_db)
) -> None:
    connection = _require(db, user, connection_id)
    name = connection.name
    try:
        svc.delete_connection(db, connection=connection)
    except svc.ConnectionRuleError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    audit.record(
        db, actor=user, action="connection.delete", target_type="connection", target_id=connection_id,
        detail={"name": name},
    )  # fmt: skip
