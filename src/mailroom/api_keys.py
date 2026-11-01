import hashlib
import secrets

_KEY_BYTES = 32
_PREFIX_LENGTH = 8


def generate_api_key() -> str:
    return f"mr_{secrets.token_urlsafe(_KEY_BYTES)}"


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


def key_prefix(raw_key: str) -> str:
    """A short, non-secret identifier for telling keys apart."""
    return raw_key[:_PREFIX_LENGTH]
