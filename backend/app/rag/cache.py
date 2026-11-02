"""Redis cache for retrieval results. Keys always carry the tenant id.

Redis being unavailable must never break chat, so every operation degrades to a miss.
"""

import hashlib
import json
import logging
from typing import Any
from uuid import UUID

import redis

from app.core.redis_client import get_redis

logger = logging.getLogger(__name__)

TTL_SECONDS = 600


def _redis() -> redis.Redis:
    return get_redis()


def cache_key(tenant_id: UUID, query: str, scope: str = "") -> str:
    """`scope` captures everything besides the query that changes the result
    (attached collections, thresholds), so different agents never share entries."""
    digest = hashlib.sha256(f"{scope}|{query.strip().lower()}".encode()).hexdigest()[:32]
    return f"rag:{tenant_id}:{digest}"


def get_cached(tenant_id: UUID, query: str, scope: str = "") -> list[dict[str, Any]] | None:
    try:
        raw = _redis().get(cache_key(tenant_id, query, scope))
    except redis.RedisError:
        logger.warning("redis unavailable on get; treating as miss")
        return None
    return json.loads(raw) if raw else None


def set_cached(tenant_id: UUID, query: str, chunks: list[dict[str, Any]], scope: str = "") -> None:
    try:
        _redis().set(cache_key(tenant_id, query, scope), json.dumps(chunks), ex=TTL_SECONDS)
    except redis.RedisError:
        logger.warning("redis unavailable on set; skipping cache")


def invalidate_tenant(tenant_id: UUID) -> None:
    """Drop every cached retrieval for one tenant (called after ingest/reindex)."""
    try:
        client = _redis()
        keys = list(client.scan_iter(match=f"rag:{tenant_id}:*", count=200))
        if keys:
            client.delete(*keys)
    except redis.RedisError:
        logger.warning("redis unavailable on invalidate")
