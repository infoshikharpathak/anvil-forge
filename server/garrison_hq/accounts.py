"""Users, sessions and the login rate limit."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .models import LoginAttempt, User, UserSession
from .security import (
    MIN_PASSWORD_LENGTH, aware, burn_password_check, hash_password, new_token, sha256, verify_password,
)
from .settings import Settings

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class AccountError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def normalize_email(email: str) -> str:
    return email.strip().lower()


def create_user(db: Session, email: str, password: str) -> User:
    email = normalize_email(email)
    if not _EMAIL.match(email):
        raise AccountError(422, "Enter a valid email address.")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AccountError(422, f"Passwords need at least {MIN_PASSWORD_LENGTH} characters.")
    if db.scalar(select(User).where(User.email == email)) is not None:
        raise AccountError(409, "An account with that email already exists.")
    user = User(email=email, password_hash=hash_password(password))
    db.add(user)
    db.flush()
    return user


def _recent_failures(db: Session, column, value: str, since: datetime) -> int:
    return db.scalar(
        select(func.count()).select_from(LoginAttempt)
        .where(column == value, LoginAttempt.success.is_(False), LoginAttempt.created_at >= since)
    ) or 0


def authenticate(db: Session, settings: Settings, email: str, password: str, ip: str) -> User:
    """Check the rate limit, then the password. Records the attempt either way."""
    email = normalize_email(email)
    now = datetime.now(timezone.utc)
    since = now - timedelta(minutes=settings.login_window_minutes)
    if (_recent_failures(db, LoginAttempt.email, email, since) >= settings.login_max_per_email
            or _recent_failures(db, LoginAttempt.ip, ip, since) >= settings.login_max_per_ip):
        raise AccountError(429, f"Too many failed sign-in attempts. Try again in {settings.login_window_minutes} minutes.")

    user = db.scalar(select(User).where(User.email == email))
    ok = verify_password(user.password_hash, password) if user else (burn_password_check(password) or False)
    db.add(LoginAttempt(email=email, ip=ip, success=bool(ok), created_at=now))
    if not ok:
        raise AccountError(401, "Email or password is incorrect.")
    return user


def start_session(db: Session, settings: Settings, user: User) -> str:
    token = new_token()
    now = datetime.now(timezone.utc)
    db.add(UserSession(user_id=user.id, token_hash=sha256(token), created_at=now, last_seen_at=now,
                       expires_at=now + timedelta(days=settings.session_days)))
    return token


def session_user(db: Session, token: str | None) -> User | None:
    """The signed-in user for a cookie token, or None. Expired sessions are deleted."""
    if not token:
        return None
    sess = db.scalar(select(UserSession).where(UserSession.token_hash == sha256(token)))
    if sess is None:
        return None
    now = datetime.now(timezone.utc)
    if aware(sess.expires_at) <= now:
        db.delete(sess)
        return None
    sess.last_seen_at = now
    return sess.user


def end_session(db: Session, token: str | None) -> None:
    if token:
        db.execute(delete(UserSession).where(UserSession.token_hash == sha256(token)))
