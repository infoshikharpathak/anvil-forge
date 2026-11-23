"""Hashing and tokens.

Passwords: argon2id (slow on purpose, salted). Session tokens and app keys
are high-entropy random strings, so a fast SHA-256 is the right hash for
them (nothing to brute-force), and it allows a direct indexed lookup.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

_hasher = PasswordHasher()

SESSION_COOKIE = "garrison_session"
CSRF_HEADER = "X-Garrison"
APP_KEY_PREFIX = "gk_"
# Keys issued before the rename from Bastion start with bk_ and keep working.
ACCEPTED_KEY_PREFIXES = (APP_KEY_PREFIX, "bk_")
MIN_PASSWORD_LENGTH = 10


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


_DUMMY_HASH = _hasher.hash("timing-equalizer-not-a-password")


def burn_password_check(password: str) -> None:
    """Spend the same time as a real check when the email is unknown."""
    verify_password(_DUMMY_HASH, password)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def new_app_key() -> str:
    return APP_KEY_PREFIX + secrets.token_urlsafe(32)


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def same(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)


def aware(dt: datetime | None) -> datetime | None:
    """The same instant as a UTC datetime. SQLite returns naive datetimes
    (everything we store is UTC); Postgres returns them in the server's
    TimeZone setting, which needn't be UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
