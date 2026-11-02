"""Authentication for the public /v1 API (secret API keys)."""

from dataclasses import dataclass
from uuid import UUID

from fastapi import Depends, Header, Request
from sqlalchemy.orm import Session

from app.core.api_errors import ApiError
from app.core.config import get_settings
from app.db.models import Agent, ApiKey
from app.db.session import get_db
from app.services import agents as agents_service


@dataclass
class ApiPrincipal:
    tenant_id: UUID
    key: ApiKey
    agent: Agent


def client_ip(request: Request) -> str | None:
    """The caller's address. Behind a trusted proxy that is the last hop it appended to X-Forwarded-For
    (anything earlier could have been written by the caller)."""
    if get_settings().trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for", "")
        hops = [h.strip() for h in forwarded.split(",") if h.strip()]
        if hops:
            return hops[-1]
    return request.client.host if request.client else None


def get_api_principal(
    agent_id: UUID,
    request: Request,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> ApiPrincipal:
    """Resolve `Authorization: Bearer sk_live_...` and check it belongs to the URL's agent."""
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise ApiError(
            401, "missing_api_key", "Send your secret key as 'Authorization: Bearer <key>'.",
            {"WWW-Authenticate": "Bearer"},
        )  # fmt: skip
    resolved = agents_service.authenticate_key(db, token.strip())
    if resolved is None:
        raise ApiError(
            401,
            "invalid_api_key",
            "The API key is invalid or has been revoked.",
            {"WWW-Authenticate": "Bearer"},
        )
    key, agent = resolved
    if not agents_service.ip_allowed(key, client_ip(request)):
        raise ApiError(403, "ip_not_allowed", "This API key cannot be used from your address.")
    if agent.id != agent_id:
        # Same answer whether the agent exists or belongs to someone else: no probing.
        raise ApiError(403, "agent_mismatch", "This API key cannot access that agent.")
    return ApiPrincipal(tenant_id=key.tenant_id, key=key, agent=agent)
