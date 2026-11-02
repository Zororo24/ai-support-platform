"""Encryption at rest for credentials companies give us (their API keys for connections).

`SECRETS_KEY` is one or more Fernet keys, comma separated. The first encrypts; all decrypt, so
keys can be rotated by prepending a new one. In development, when it is unset, a key is derived
from JWT_SECRET so nothing needs configuring, but production must set its own.
"""

import base64
import hashlib
import logging
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class SecretError(Exception):
    """A stored secret could not be decrypted (wrong or missing key)."""


@lru_cache
def _fernet_for(configured: str, jwt_secret: str) -> MultiFernet:
    keys = [k.strip() for k in configured.split(",") if k.strip()]
    if not keys:
        logger.warning("SECRETS_KEY is not set; deriving a development key from JWT_SECRET")
        derived = base64.urlsafe_b64encode(hashlib.sha256(f"secrets:{jwt_secret}".encode()).digest())
        keys = [derived.decode()]
    return MultiFernet([Fernet(k.encode()) for k in keys])


def _fernet() -> MultiFernet:
    settings = get_settings()
    return _fernet_for(settings.secrets_key, settings.jwt_secret)


def encrypt(plain: str) -> str:
    return _fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise SecretError("stored secret cannot be decrypted with the configured SECRETS_KEY") from exc


def hint(plain: str) -> str:
    """What the dashboard may show afterwards: only the last four characters."""
    return f"…{plain[-4:]}" if len(plain) >= 8 else "••••"
