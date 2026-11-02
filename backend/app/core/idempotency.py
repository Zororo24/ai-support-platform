"""Idempotency for the message endpoint (Redis, 24h).

A retried request with the same Idempotency-Key and the same body returns the stored response
instead of running the agent twice. Fails open if Redis is down (the request just runs).
"""

import hashlib
import json
import logging
from typing import Any
from uuid import UUID

import redis

from app.core.redis_client import get_redis

logger = logging.getLogger(__name__)

PENDING_TTL = 120
DONE_TTL = 24 * 3600


def _key(tenant_id: UUID, api_key_id: UUID, idem_key: str) -> str:
    return f"idem:{tenant_id}:{api_key_id}:{hashlib.sha256(idem_key.encode()).hexdigest()[:32]}"


def fingerprint(body: Any) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[:32]


def begin(tenant_id: UUID, api_key_id: UUID, idem_key: str, body_fp: str) -> tuple[str, dict | None]:
    """Returns ("new", None) | ("replay", stored_response) | ("in_progress", None) | ("conflict", None)."""
    key = _key(tenant_id, api_key_id, idem_key)
    try:
        client = get_redis()
        pending = json.dumps({"state": "pending", "fp": body_fp})
        if client.set(key, pending, nx=True, ex=PENDING_TTL):
            return "new", None
        raw = client.get(key)
        if raw is None:  # expired between the two calls
            return "new", None
        stored = json.loads(raw)
        if stored.get("fp") != body_fp:
            return "conflict", None
        if stored.get("state") == "pending":
            return "in_progress", None
        return "replay", stored["response"]
    except redis.RedisError:
        logger.warning("idempotency: redis unavailable, running without replay protection")
        return "new", None


def complete(tenant_id: UUID, api_key_id: UUID, idem_key: str, body_fp: str, response: dict) -> None:
    try:
        get_redis().set(
            _key(tenant_id, api_key_id, idem_key),
            json.dumps({"state": "done", "fp": body_fp, "response": response}),
            ex=DONE_TTL,
        )
    except redis.RedisError:
        pass


def abort(tenant_id: UUID, api_key_id: UUID, idem_key: str) -> None:
    """Forget a failed attempt so the client's retry can run."""
    try:
        get_redis().delete(_key(tenant_id, api_key_id, idem_key))
    except redis.RedisError:
        pass
