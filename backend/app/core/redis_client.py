import redis

from app.core.config import get_settings

_client: redis.Redis | None = None


def get_redis() -> redis.Redis:
    """Shared client. Short timeouts: a slow Redis must never stall a request."""
    global _client
    if _client is None:
        _client = redis.Redis.from_url(
            get_settings().redis_url, socket_connect_timeout=0.5, socket_timeout=0.5
        )
    return _client
