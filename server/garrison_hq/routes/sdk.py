"""SDK API (/v1/sdk): app-key auth, read-only.

An app key reaches only this router. It can't sign in, and the management
routes ignore the Authorization header entirely (they need a session).
Session cookies don't work here either: SDK calls must present a key.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from sqlalchemy.orm import Session

from ..deps import get_db
from ..keys import app_for_key
from ..models import App
from ..workspace import build_bundle, etag_of, latest_revision

router = APIRouter(prefix="/v1/sdk", tags=["sdk"])


def key_app(authorization: str | None = Header(default=None), db: Session = Depends(get_db)) -> App:
    raw = authorization[7:].strip() if authorization and authorization.lower().startswith("bearer ") else None
    found = app_for_key(db, raw)
    if found is None:
        raise HTTPException(401, "A valid app key is required (Authorization: Bearer gk_...).",
                            headers={"WWW-Authenticate": "Bearer"})
    return found[0]


def _etag_matches(header: str | None, etag: str) -> bool:
    if not header:
        return False
    if header.strip() == "*":
        return True
    tags = [t.strip().removeprefix("W/").strip('"') for t in header.split(",")]
    return etag in tags


@router.get("/bundle")
def bundle(request: Request, response: Response, app: App = Depends(key_app), db: Session = Depends(get_db)):
    """The app's bundle in exactly the SDK's config format, with its revision and ETag."""
    rev = latest_revision(db, app)
    data = rev.bundle if rev else build_bundle(db, app)
    etag = rev.etag if rev else etag_of(data)
    headers = {"ETag": f'"{etag}"', "Cache-Control": "no-cache"}
    if _etag_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    response.headers.update(headers)
    return {"app": app.name, "revision": rev.revision if rev else 0, "etag": etag, "bundle": data}
