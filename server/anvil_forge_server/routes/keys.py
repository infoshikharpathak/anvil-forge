"""App key management (/v1/apps/{app}/keys): create (shown once), list, revoke."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..deps import csrf, current_user, get_db
from ..keys import create_key, key_view
from ..models import AppKey, User
from ..workspace import NotFound, commit_change, user_app

router = APIRouter(prefix="/v1/apps/{app_name}/keys", tags=["keys"], dependencies=[Depends(csrf)])


class KeyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(default="", max_length=100)


@router.get("")
def list_keys(app_name: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    app = user_app(db, user, app_name)
    keys = db.scalars(select(AppKey).where(AppKey.app_id == app.id).order_by(AppKey.id.desc())).all()
    return [key_view(k) for k in keys]


@router.post("", status_code=201)
def new_key(app_name: str, body: KeyIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    app = user_app(db, user, app_name)
    key, raw = create_key(db, user, app, body.name)
    commit_change(db, user, [], action="create", entity_type="app_key", entity_name=f"{app.name}:{key.prefix}",
                  audit_app=app, after={"name": key.name, "prefix": key.prefix})
    return {**key_view(key), "key": raw, "note": "Copy this key now. It won't be shown again."}


@router.delete("/{key_id}")
def revoke_key(app_name: str, key_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    app = user_app(db, user, app_name)
    key = db.scalar(select(AppKey).where(AppKey.id == key_id, AppKey.app_id == app.id))
    if key is None:
        raise NotFound(f"Key {key_id} not found in app {app.name!r}.")
    if key.revoked_at is None:
        key.revoked_at = datetime.now(timezone.utc)
        commit_change(db, user, [], action="revoke", entity_type="app_key", entity_name=f"{app.name}:{key.prefix}",
                      audit_app=app, before={"prefix": key.prefix, "active": True},
                      after={"prefix": key.prefix, "active": False})
    return key_view(key)
