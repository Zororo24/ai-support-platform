"""API key generation and hashing. Only the SHA-256 of a key is ever stored."""

import hashlib
import secrets

KEY_PREFIX = "sk_live_"
DISPLAY_PREFIX_CHARS = 12


def generate_key() -> tuple[str, str, str]:
    """Return (full_key, display_prefix, sha256_hex). The full key is shown to the user once."""
    full = KEY_PREFIX + secrets.token_urlsafe(32)
    return full, full[:DISPLAY_PREFIX_CHARS], hash_key(full)


def hash_key(key: str) -> str:
    # Keys are 256-bit random values, so a fast hash is sufficient (unlike passwords).
    return hashlib.sha256(key.encode("utf-8")).hexdigest()
