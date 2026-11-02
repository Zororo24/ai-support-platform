"""Fixed-window rate limiting in Redis.

Protects the (free-tier) LLM quota and login from brute force. If Redis is unavailable the
limiter fails OPEN: availability beats throttling, and the failure is logged.
Keys carry the tenant id wherever one is known; login is pre-auth so it is keyed by email/IP.
"""

import logging
import time
from collections.abc import Callable

import redis
from fastapi import Depends, HTTPException, status

from app.api.deps import get_current_user
from app.core.config import get_settings
from app.core.redis_client import get_redis
from app.db.models import User

logger = logging.getLogger(__name__)


def hit(scope: str, *, limit: int, window: int) -> int | None:
    """Count one event in `scope`. Returns None if allowed, else seconds until the window resets."""
    now = time.time()
    bucket = int(now // window)
    key = f"rl:{scope}:{bucket}"
    try:
        client = get_redis()
        pipe = client.pipeline()
        pipe.incr(key)
        pipe.expire(key, window + 1)
        count = int(pipe.execute()[0])
    except redis.RedisError:
        logger.warning("rate limiter: redis unavailable, allowing request (scope=%s)", scope)
        return None
    if count > limit:
        return max(1, int(window - (now % window)))
    return None


def raise_if_limited(retry_after: int | None) -> None:
    if retry_after is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many requests. Please try again in {retry_after} seconds.",
            headers={"Retry-After": str(retry_after)},
        )


def rate_limited(name: str, *, per_user: str, per_tenant: str | None = None, window: int = 60) -> Callable:
    """Dependency factory. Limits are read from settings by attribute name at request time,
    so they can be tuned (and tests can lower them) without re-importing."""

    def dependency(user: User = Depends(get_current_user)) -> User:
        settings = get_settings()
        raise_if_limited(
            hit(
                f"{user.tenant_id}:{name}:user:{user.id}",
                limit=getattr(settings, per_user),
                window=window,
            )
        )
        if per_tenant:
            raise_if_limited(
                hit(
                    f"{user.tenant_id}:{name}:tenant",
                    limit=getattr(settings, per_tenant),
                    window=window,
                )
            )
        return user

    return dependency


# ---- helpers for the public /v1 API -------------------------------------------------------


def hit_detailed(scope: str, *, limit: int, window: int) -> tuple[bool, int, int]:
    """Like `hit`, but also reports headroom: returns (allowed, remaining, seconds_until_reset).

    Fails open (allowed, full headroom) if Redis is unavailable.
    """
    now = time.time()
    reset = max(1, int(window - (now % window)))
    key = f"rl:{scope}:{int(now // window)}"
    try:
        pipe = get_redis().pipeline()
        pipe.incr(key)
        pipe.expire(key, window + 1)
        count = int(pipe.execute()[0])
    except redis.RedisError:
        logger.warning("rate limiter: redis unavailable, allowing request (scope=%s)", scope)
        return True, limit, reset
    return count <= limit, max(0, limit - count), reset


def acquire_slot(scope: str, *, limit: int, ttl: int = 180) -> bool:
    """Take one of `limit` concurrent slots. The TTL frees slots leaked by a crashed worker."""
    key = f"conc:{scope}"
    try:
        client = get_redis()
        count = int(client.incr(key))
        client.expire(key, ttl)
        if count > limit:
            client.decr(key)
            return False
    except redis.RedisError:
        logger.warning("concurrency limiter: redis unavailable, allowing (scope=%s)", scope)
    return True


def release_slot(scope: str) -> None:
    try:
        client = get_redis()
        if int(client.decr(f"conc:{scope}")) < 0:
            client.set(f"conc:{scope}", 0, ex=180)
    except redis.RedisError:
        pass


def acquire_lock(scope: str, *, ttl: int = 120):
    """Non-blocking lock. Returns a lock object to release, True if Redis is down (fail open),
    or None if someone else holds it."""
    try:
        lock = get_redis().lock(f"lock:{scope}", timeout=ttl, blocking=False)
        return lock if lock.acquire(blocking=False) else None
    except redis.RedisError:
        logger.warning("session lock: redis unavailable, proceeding without lock (scope=%s)", scope)
        return True


def release_lock(lock) -> None:
    if lock is None or lock is True:
        return
    try:
        lock.release()
    except (redis.RedisError, redis.exceptions.LockError):
        pass
