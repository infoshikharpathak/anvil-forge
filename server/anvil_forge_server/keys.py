"""App keys: high-entropy bearer tokens for the SDK, one app each."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import App, AppKey, User
from .security import APP_KEY_PREFIX, aware, new_app_key, sha256

PREFIX_LEN = 12  # "afk_" + 8 chars: enough to tell keys apart, useless to an attacker
_TOUCH_EVERY = timedelta(seconds=60)


def create_key(db: Session, user: User, app: App, name: str = "") -> tuple[AppKey, str]:
    """Returns the row and the raw key. The raw key is never stored or shown again."""
    raw = new_app_key()
    key = AppKey(app_id=app.id, name=name, key_hash=sha256(raw), prefix=raw[:PREFIX_LEN], created_by=user.id)
    db.add(key)
    db.flush()
    return key, raw


def app_for_key(db: Session, raw: str | None) -> tuple[App, AppKey] | None:
    """The app a live key belongs to, or None (unknown, malformed or revoked)."""
    if not raw or not raw.startswith(APP_KEY_PREFIX):
        return None
    key = db.scalar(select(AppKey).where(AppKey.key_hash == sha256(raw)))
    if key is None or key.revoked_at is not None:
        return None
    now = datetime.now(timezone.utc)
    if key.last_used_at is None or now - aware(key.last_used_at) >= _TOUCH_EVERY:
        key.last_used_at = now
    return db.get(App, key.app_id), key


def key_view(key: AppKey) -> dict:
    return {"id": key.id, "name": key.name, "prefix": key.prefix, "created_at": key.created_at,
            "last_used_at": key.last_used_at, "revoked_at": key.revoked_at,
            "active": key.revoked_at is None}
