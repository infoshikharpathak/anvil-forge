"""Sign up, log in, log out, who am I."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..accounts import AccountError, authenticate, create_user, end_session, start_session
from ..deps import client_ip, csrf, current_user, get_db, get_settings
from ..models import User
from ..security import SESSION_COOKIE
from ..settings import Settings

router = APIRouter(prefix="/v1/auth", tags=["auth"], dependencies=[Depends(csrf)])


class Credentials(BaseModel):
    email: str
    password: str


def _set_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        SESSION_COOKIE, token, max_age=settings.session_days * 86400,
        httponly=True, samesite="strict", secure=settings.secure_cookies, path="/",
    )


def _user_view(user: User) -> dict:
    return {"id": user.id, "email": user.email}


@router.post("/signup", status_code=201)
def signup(body: Credentials, response: Response, db: Session = Depends(get_db),
           settings: Settings = Depends(get_settings)):
    if not settings.signup_open:
        raise HTTPException(403, "Sign-up is closed. Ask an admin to create your account.")
    try:
        user = create_user(db, body.email, body.password)
    except AccountError as e:
        raise HTTPException(e.status, e.message) from None
    _set_cookie(response, start_session(db, settings, user), settings)
    return _user_view(user)


@router.post("/login")
def login(body: Credentials, request: Request, response: Response, db: Session = Depends(get_db),
          settings: Settings = Depends(get_settings), ip: str = Depends(client_ip)):
    try:
        user = authenticate(db, settings, body.email, body.password, ip)
    except AccountError as e:
        db.commit()  # keep the failed attempt even though we're returning an error
        raise HTTPException(e.status, e.message) from None
    _set_cookie(response, start_session(db, settings, user), settings)
    return _user_view(user)


@router.post("/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    end_session(db, request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
def me(user: User = Depends(current_user)):
    return _user_view(user)
