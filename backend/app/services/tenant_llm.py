"""A workspace's own AI provider key.

By default the platform's key is used for everything. A workspace can supply its own key (same
provider, same models) so its usage is billed to its own account. The key is stored encrypted with
the secrets vault, shown only as a hint, and used for that workspace's conversations, indexing,
tests and quality checks. If it can no longer be read the call fails loudly rather than quietly
falling back to the platform's key (which would bill the wrong account).
"""

import logging
import time
from uuid import UUID

from sqlalchemy.orm import Session

from app.core import secrets as vault
from app.core.llm import LLMUnavailable, bind_usage, embed, new_usage
from app.db.models import Tenant

logger = logging.getLogger(__name__)

_CACHE_SECONDS = 30
_cache: dict[UUID, tuple[float, str | None]] = {}


class KeyRejected(Exception):
    """The provider did not accept the key."""


def _forget(tenant_id: UUID) -> None:
    _cache.pop(tenant_id, None)


def get_key(db: Session, tenant_id: UUID) -> str | None:
    cached = _cache.get(tenant_id)
    if cached is not None and cached[0] > time.monotonic():
        return cached[1]
    tenant = db.get(Tenant, tenant_id)
    key: str | None = None
    if tenant is not None and tenant.llm_api_key_encrypted:
        try:
            key = vault.decrypt(tenant.llm_api_key_encrypted)
        except Exception as exc:
            logger.error("workspace %s: its AI provider key cannot be decrypted", tenant_id)
            raise LLMUnavailable("This workspace's AI provider key cannot be read. Set it again.") from exc
    _cache[tenant_id] = (time.monotonic() + _CACHE_SECONDS, key)
    return key


def bind(db: Session, tenant_id: UUID) -> dict[str, int]:
    """Start counting model usage on this context, with the workspace's own key if it has one."""
    usage = new_usage(get_key(db, tenant_id))
    bind_usage(usage)
    return usage


def status(db: Session, tenant_id: UUID) -> dict[str, object]:
    tenant = db.get(Tenant, tenant_id)
    return {
        "configured": bool(tenant and tenant.llm_api_key_encrypted),
        "hint": tenant.llm_key_hint if tenant else None,
    }


def set_key(db: Session, *, tenant_id: UUID, api_key: str) -> None:
    api_key = api_key.strip()
    if len(api_key) < 8:
        raise KeyRejected("That does not look like an API key.")
    usage = new_usage(api_key)
    bind_usage(usage)
    try:
        embed(["connection check"])  # the cheapest call that proves the key works
    except LLMUnavailable as exc:
        raise KeyRejected("The AI provider did not accept that key.") from exc
    tenant = db.get(Tenant, tenant_id)
    tenant.llm_api_key_encrypted = vault.encrypt(api_key)
    tenant.llm_key_hint = vault.hint(api_key)
    db.commit()
    _forget(tenant_id)


def clear_key(db: Session, *, tenant_id: UUID) -> None:
    tenant = db.get(Tenant, tenant_id)
    tenant.llm_api_key_encrypted = None
    tenant.llm_key_hint = None
    db.commit()
    _forget(tenant_id)
