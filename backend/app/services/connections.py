import logging
import re
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.action_schema import FORBIDDEN_HEADERS
from app.agents.executor import ConnectionInfo
from app.core import secrets, ssrf
from app.db.models import Agent, AgentVersion, Connection

logger = logging.getLogger(__name__)

AUTH_TYPES = ("none", "bearer", "header", "basic")


class ConnectionRuleError(Exception):
    """A refused connection change; the message is safe to show the admin."""


class ConnectionNameTaken(ConnectionRuleError):
    """Another connection in the workspace already has that name."""


def get_connection(db: Session, *, tenant_id: UUID, connection_id: UUID) -> Connection | None:
    connection = db.get(Connection, connection_id)
    return connection if connection is not None and connection.tenant_id == tenant_id else None


def list_connections(db: Session, *, tenant_id: UUID) -> list[Connection]:
    return list(
        db.scalars(
            select(Connection).where(Connection.tenant_id == tenant_id).order_by(Connection.created_at)
        )
    )


def _validate(
    base_url: str, auth_type: str, header_name: str | None, secret: str | None, *, need_secret: bool
):
    base_url = base_url.strip().rstrip("/")
    try:
        ssrf.validate_url(base_url + "/")
    except ssrf.BlockedDestination as exc:
        raise ConnectionRuleError(f"base_url: {exc}") from exc
    if "?" in base_url or "#" in base_url:
        raise ConnectionRuleError("base_url must not contain a query string or fragment")
    if auth_type not in AUTH_TYPES:
        raise ConnectionRuleError(f"auth_type must be one of: {', '.join(AUTH_TYPES)}")
    if auth_type == "header":
        if not header_name or not re.fullmatch(r"[A-Za-z0-9\-]{1,60}", header_name):
            raise ConnectionRuleError("auth_header_name is required for header authentication")
        if header_name.lower() in FORBIDDEN_HEADERS - {"authorization"}:
            raise ConnectionRuleError(f"'{header_name}' cannot be used as an authentication header")
    if auth_type != "none" and need_secret and not secret:
        raise ConnectionRuleError("a secret is required for this authentication type")
    if auth_type == "basic" and secret and ":" not in secret:
        raise ConnectionRuleError("basic authentication secrets must look like username:password")
    return base_url


def create_connection(
    db: Session,
    *,
    tenant_id: UUID,
    user_id: UUID | None,
    name: str,
    base_url: str,
    auth_type: str,
    auth_header_name: str | None,
    secret: str | None,
) -> Connection:
    name = name.strip()
    base_url = _validate(base_url, auth_type, auth_header_name, secret, need_secret=True)
    taken = db.scalars(
        select(Connection).where(
            Connection.tenant_id == tenant_id, func.lower(Connection.name) == name.lower()
        )
    ).first()
    if taken is not None:
        raise ConnectionNameTaken("A connection with that name already exists")
    connection = Connection(
        tenant_id=tenant_id,
        name=name,
        base_url=base_url,
        auth_type=auth_type,
        auth_header_name=auth_header_name if auth_type == "header" else None,
        secret_encrypted=secrets.encrypt(secret) if auth_type != "none" and secret else None,
        secret_hint=secrets.hint(secret) if auth_type != "none" and secret else None,
        created_by=user_id,
    )
    db.add(connection)
    db.commit()
    db.refresh(connection)
    return connection


def update_connection(
    db: Session,
    *,
    connection: Connection,
    name: str | None,
    base_url: str | None,
    auth_type: str | None,
    auth_header_name: str | None,
    secret: str | None,
) -> Connection:
    new_auth = auth_type or connection.auth_type
    new_header = auth_header_name if auth_header_name is not None else connection.auth_header_name
    # Switching auth type requires supplying a new secret; otherwise the stored one stays.
    needs_new_secret = new_auth != connection.auth_type and new_auth != "none"
    new_url = _validate(
        base_url or connection.base_url, new_auth, new_header, secret, need_secret=needs_new_secret
    )
    if name is not None and name.strip().lower() != connection.name.lower():
        taken = db.scalars(
            select(Connection).where(
                Connection.tenant_id == connection.tenant_id,
                func.lower(Connection.name) == name.strip().lower(),
                Connection.id != connection.id,
            )
        ).first()
        if taken is not None:
            raise ConnectionNameTaken("A connection with that name already exists")
    if name is not None:
        connection.name = name.strip()
    connection.base_url = new_url
    connection.auth_type = new_auth
    connection.auth_header_name = new_header if new_auth == "header" else None
    if new_auth == "none":
        connection.secret_encrypted, connection.secret_hint = None, None
    elif secret:
        connection.secret_encrypted, connection.secret_hint = secrets.encrypt(secret), secrets.hint(secret)
    db.commit()
    db.refresh(connection)
    return connection


def used_by(db: Session, *, tenant_id: UUID, connection_id: UUID) -> list[str]:
    """Names of agents whose draft or any published version has an action on this connection."""
    needle = str(connection_id)

    def uses(config: dict[str, Any] | None) -> bool:
        return any(str(a.get("connection_id")) == needle for a in (config or {}).get("actions", []))

    names: list[str] = []
    for agent in db.scalars(select(Agent).where(Agent.tenant_id == tenant_id)):
        in_versions = any(
            uses(v.config) for v in db.scalars(select(AgentVersion).where(AgentVersion.agent_id == agent.id))
        )
        if uses(agent.draft_config) or in_versions:
            names.append(agent.name)
    return names


def delete_connection(db: Session, *, connection: Connection) -> None:
    users = used_by(db, tenant_id=connection.tenant_id, connection_id=connection.id)
    if users:
        raise ConnectionRuleError(f"Still used by: {', '.join(users)}. Remove those actions first.")
    db.delete(connection)
    db.commit()


def load_info(db: Session, *, tenant_id: UUID, connection_id: UUID) -> ConnectionInfo | None:
    """Decrypted connection details for one call; None if missing or unreadable."""
    connection = get_connection(db, tenant_id=tenant_id, connection_id=connection_id)
    if connection is None:
        return None
    secret = None
    if connection.secret_encrypted:
        try:
            secret = secrets.decrypt(connection.secret_encrypted)
        except secrets.SecretError:
            logger.error("cannot decrypt the secret of connection %s", connection.id)
            return None
    return ConnectionInfo(
        base_url=connection.base_url,
        auth_type=connection.auth_type,
        auth_header_name=connection.auth_header_name,
        secret=secret,
    )
