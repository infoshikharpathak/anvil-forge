"""Request dependencies: DB session, signed-in user, CSRF check, client IP."""

from __future__ import annotations

from typing import Iterator

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .accounts import session_user
from .models import User
from garrison.compat import header

from .security import CSRF_HEADER, SESSION_COOKIE
from .settings import Settings

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_db(request: Request) -> Iterator[Session]:
    """One transaction per request: committed on success, rolled back on error."""
    factory = request.app.state.sessionmaker
    with factory() as db:
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise


def csrf(request: Request) -> None:
    """Writes need the CSRF header. A cross-site form or plain link can't
    set custom headers, so a forged request from another site fails here."""
    if request.method not in SAFE_METHODS and header(request.headers, CSRF_HEADER) != "1":
        raise HTTPException(403, f"Missing {CSRF_HEADER}: 1 header.")


def client_ip(request: Request, settings: Settings = Depends(get_settings)) -> str:
    if settings.trust_proxy and (fwd := request.headers.get("x-forwarded-for")):
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user = session_user(db, request.cookies.get(SESSION_COOKIE))
    if user is None:
        db.commit()  # keep the deletion of an expired session; the 401 below rolls back otherwise
        raise HTTPException(401, "Sign in first.", headers={"WWW-Authenticate": "Session"})
    return user
