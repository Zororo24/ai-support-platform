import ipaddress
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.config import AgentConfig, parse_config
from app.core.apikeys import generate_key, hash_key
from app.db.models import Agent, AgentVersion, ApiKey, Collection, Connection, User
from app.services import collections as collections_service

LAST_USED_THROTTLE = timedelta(seconds=60)


class AgentError(Exception):
    """A refused agent operation; the message is safe to show to the admin."""


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:80] or "agent"


# ---------- agents ----------


def get_agent(db: Session, *, tenant_id: UUID, agent_id: UUID) -> Agent | None:
    agent = db.get(Agent, agent_id)
    return agent if agent is not None and agent.tenant_id == tenant_id else None


def list_agents(db: Session, *, tenant_id: UUID) -> list[Agent]:
    return list(db.scalars(select(Agent).where(Agent.tenant_id == tenant_id).order_by(Agent.created_at)))


def _unique_slug(db: Session, tenant_id: UUID, name: str) -> str:
    base = slugify(name)
    slug, n = base, 1
    while db.scalars(select(Agent).where(Agent.tenant_id == tenant_id, Agent.slug == slug)).first():
        n += 1
        slug = f"{base[:74]}-{n}"
    return slug


def validate_config(db: Session, *, tenant_id: UUID, raw: dict[str, Any]) -> AgentConfig:
    """Parse and check a config. Raises AgentError with a readable message."""
    try:
        config = parse_config(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first["loc"])
        raise AgentError(f"{where}: {first['msg']}") from exc
    ids = list(dict.fromkeys(config.knowledge.collection_ids))
    if ids:
        found = set(
            db.scalars(select(Collection.id).where(Collection.tenant_id == tenant_id, Collection.id.in_(ids)))
        )
        missing = [str(i) for i in ids if i not in found]
        if missing:
            raise AgentError(f"knowledge.collection_ids: unknown collection {missing[0]}")
    connection_ids = list(dict.fromkeys(a.connection_id for a in config.actions if a.connection_id))
    if connection_ids:
        known = set(
            db.scalars(
                select(Connection.id).where(
                    Connection.tenant_id == tenant_id, Connection.id.in_(connection_ids)
                )
            )
        )
        for action in config.actions:
            if action.connection_id is not None and action.connection_id not in known:
                raise AgentError(
                    f"actions.{action.name}.connection_id: unknown connection {action.connection_id}"
                )
    return config


def create_agent(
    db: Session,
    *,
    user: User | None,
    tenant_id: UUID,
    name: str,
    description: str = "",
    config: dict[str, Any] | None = None,
) -> Agent:
    validated = validate_config(db, tenant_id=tenant_id, raw=config or {})
    agent = Agent(
        tenant_id=tenant_id,
        name=name.strip(),
        slug=_unique_slug(db, tenant_id, name),
        description=description.strip(),
        draft_config=validated.model_dump(mode="json"),
        created_by=user.id if user else None,
    )
    db.add(agent)
    db.commit()
    db.refresh(agent)
    return agent


def update_agent(db: Session, *, agent: Agent, name: str | None, description: str | None) -> Agent:
    if name is not None:
        agent.name = name.strip()
    if description is not None:
        agent.description = description.strip()
    db.commit()
    db.refresh(agent)
    return agent


def save_draft(db: Session, *, agent: Agent, raw: dict[str, Any]) -> Agent:
    validated = validate_config(db, tenant_id=agent.tenant_id, raw=raw)
    agent.draft_config = validated.model_dump(mode="json")
    db.commit()
    db.refresh(agent)
    return agent


def delete_agent(db: Session, *, agent: Agent) -> None:
    db.delete(agent)  # versions, keys, sessions and their conversations cascade
    db.commit()


# ---------- versions ----------


def list_versions(db: Session, *, agent: Agent) -> list[AgentVersion]:
    return list(
        db.scalars(
            select(AgentVersion)
            .where(AgentVersion.agent_id == agent.id, AgentVersion.tenant_id == agent.tenant_id)
            .order_by(AgentVersion.version.desc())
        )
    )


def get_version(db: Session, *, agent: Agent, version: int) -> AgentVersion | None:
    return db.scalars(
        select(AgentVersion).where(
            AgentVersion.agent_id == agent.id,
            AgentVersion.tenant_id == agent.tenant_id,
            AgentVersion.version == version,
        )
    ).first()


def publish(
    db: Session,
    *,
    agent: Agent,
    user: User | None,
    note: str = "",
    test_summary: dict[str, Any] | None = None,
) -> AgentVersion:
    """Snapshot the draft as the next immutable version and make it live."""
    config = validate_config(db, tenant_id=agent.tenant_id, raw=agent.draft_config)
    latest = db.scalar(select(func.max(AgentVersion.version)).where(AgentVersion.agent_id == agent.id)) or 0
    version = AgentVersion(
        tenant_id=agent.tenant_id,
        agent_id=agent.id,
        version=latest + 1,
        config=config.model_dump(mode="json"),
        note=note.strip()[:300],
        test_summary=test_summary,
        created_by=user.id if user else None,
    )
    db.add(version)
    agent.published_version = version.version
    db.commit()
    db.refresh(version)
    return version


def make_live(db: Session, *, agent: Agent, version: int) -> AgentVersion:
    """Roll back (or forward) to an existing version without creating a new one."""
    target = get_version(db, agent=agent, version=version)
    if target is None:
        raise AgentError("That version does not exist")
    agent.published_version = target.version
    db.commit()
    return target


def live_config(db: Session, *, agent: Agent) -> tuple[AgentConfig, int] | None:
    if agent.published_version is None:
        return None
    version = get_version(db, agent=agent, version=agent.published_version)
    if version is None:
        return None
    return parse_config(version.config), version.version


def ensure_default_agent(db: Session, *, tenant_id: UUID, user: User | None = None) -> Agent:
    """A published 'Support assistant' on the tenant's default collection (idempotent)."""
    existing = db.scalars(
        select(Agent).where(Agent.tenant_id == tenant_id).order_by(Agent.created_at)
    ).first()
    if existing is not None:
        return existing
    collection = collections_service.default_collection(db, tenant_id=tenant_id)
    agent = create_agent(
        db,
        user=user,
        tenant_id=tenant_id,
        name="Support assistant",
        description="Answers customer questions from your documents.",
        config={
            "instructions": "Be concise, friendly and accurate.",
            "knowledge": {"collection_ids": [str(collection.id)]},
        },
    )
    publish(db, agent=agent, user=user, note="Initial version")
    return agent


# ---------- API keys ----------


MAX_ALLOWED_IPS = 20


def parse_allowed_ips(values: list[str]) -> list[str]:
    """Normalise a list of addresses or ranges ("203.0.113.7", "10.0.0.0/8"); raises AgentError."""
    networks: list[str] = []
    for value in values:
        text = value.strip()
        if not text:
            continue
        try:
            network = str(ipaddress.ip_network(text, strict=False))
        except ValueError as exc:
            raise AgentError(
                f"'{text}' is not an IP address or range (for example 203.0.113.7 or 203.0.113.0/24)"
            ) from exc
        if network not in networks:
            networks.append(network)
    if len(networks) > MAX_ALLOWED_IPS:
        raise AgentError(f"A key can be limited to at most {MAX_ALLOWED_IPS} addresses or ranges")
    return networks


def ip_allowed(key: ApiKey, address: str | None) -> bool:
    """An empty allow-list means any address; otherwise the caller must be inside one of the ranges."""
    if not key.allowed_ips:
        return True
    try:
        caller = ipaddress.ip_address(address or "")
    except ValueError:
        return False
    return any(caller in ipaddress.ip_network(net) for net in key.allowed_ips)


def set_key_ips(db: Session, *, agent: Agent, key_id: UUID, allowed_ips: list[str]) -> ApiKey | None:
    key = db.get(ApiKey, key_id)
    if key is None or key.agent_id != agent.id or key.tenant_id != agent.tenant_id:
        return None
    key.allowed_ips = parse_allowed_ips(allowed_ips)
    db.commit()
    db.refresh(key)
    return key


def create_key(
    db: Session,
    *,
    agent: Agent,
    user: User | None,
    name: str,
    allowed_ips: list[str] | None = None,
) -> tuple[ApiKey, str]:
    networks = parse_allowed_ips(allowed_ips or [])
    full, prefix, digest = generate_key()
    key = ApiKey(
        tenant_id=agent.tenant_id,
        agent_id=agent.id,
        name=name.strip(),
        prefix=prefix,
        key_hash=digest,
        allowed_ips=networks,
        created_by=user.id if user else None,
    )
    db.add(key)
    db.commit()
    db.refresh(key)
    return key, full


def list_keys(db: Session, *, agent: Agent) -> list[ApiKey]:
    return list(
        db.scalars(
            select(ApiKey)
            .where(ApiKey.agent_id == agent.id, ApiKey.tenant_id == agent.tenant_id)
            .order_by(ApiKey.created_at.desc())
        )
    )


def revoke_key(db: Session, *, agent: Agent, key_id: UUID) -> ApiKey | None:
    key = db.get(ApiKey, key_id)
    if key is None or key.agent_id != agent.id or key.tenant_id != agent.tenant_id:
        return None
    if key.revoked_at is None:
        key.revoked_at = datetime.now(timezone.utc)
        db.commit()
    return key


def authenticate_key(db: Session, raw_key: str) -> tuple[ApiKey, Agent] | None:
    """Resolve a presented secret key. Revoked keys and keys of deleted agents fail."""
    key = db.scalars(select(ApiKey).where(ApiKey.key_hash == hash_key(raw_key))).first()
    if key is None or key.revoked_at is not None:
        return None
    agent = db.get(Agent, key.agent_id)
    if agent is None or agent.tenant_id != key.tenant_id:
        return None
    now = datetime.now(timezone.utc)
    if key.last_used_at is None or now - key.last_used_at > LAST_USED_THROTTLE:
        key.last_used_at = now  # throttled so busy keys don't cause a write per request
        db.commit()
    return key, agent
