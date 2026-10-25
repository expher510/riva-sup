import hashlib
import secrets
from typing import Tuple

API_KEY_PREFIX = "riva_live_"

def generate_api_key() -> Tuple[str, str, str]:
    """
    Generates a secure API key.
    Returns:
        (raw_key, prefix, key_hash)
    """
    random_part = secrets.token_urlsafe(32)
    raw_key = f"{API_KEY_PREFIX}{random_part}"
    prefix = raw_key[:12]
    key_hash = hash_api_key(raw_key)
    return raw_key, prefix, key_hash

def hash_api_key(api_key: str) -> str:
    """Calculates SHA-256 hash of API key for safe database storage."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()
