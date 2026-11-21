"""Server-rendered pages (/ui). Reads come straight from the DB; every write
the pages make goes through the /v1 API with the CSRF header (static/app.js)."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..accounts import session_user
from ..deps import get_db
from ..keys import key_view
from ..models import Agent, App, AppKey, AuditLog, Flow, Grant, PromptVersion, Tool
from ..security import SESSION_COOKIE, aware
from ..workspace import latest_revision, user_app
from .manage import agent_view, version_view

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
router = APIRouter(include_in_schema=False)


def _ms(value) -> str:
    if value is None:
        return "—"
    value = float(value)
    return f"{value:.0f} ms" if value < 1000 else f"{value / 1000:.2f} s" if value < 60_000 else f"{value / 60_000:.1f} min"


def _dt(value, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """UTC, whatever timezone the database handed back."""
    return aware(value).strftime(fmt) if value else "—"


def _num(value) -> str:
    return f"{int(value or 0):,}"


def _short(value, n: int = 12) -> str:
    value = str(value or "")
    return value if len(value) <= n else value[: n - 1] + "…"


templates.env.filters.update(ms=_ms, dt=_dt, num=_num, short=_short)


def _page(request: Request, name: str, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx)


def _login_redirect(request: Request) -> RedirectResponse:
    return RedirectResponse(f"/ui/login?next={quote(request.url.path)}", status_code=303)


@router.get("/")
def root():
    return RedirectResponse("/ui", status_code=303)


@router.get("/ui/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/ui"):
    safe_next = next if next.startswith("/ui") else "/ui"  # never redirect off-site
    return _page(request, "login.html", user=None, next=safe_next,
                 signup_open=request.app.state.settings.signup_open)


@router.get("/ui/signup", response_class=HTMLResponse)
def signup_page(request: Request):
    return _page(request, "signup.html", user=None, signup_open=request.app.state.settings.signup_open)


@router.get("/ui", response_class=HTMLResponse)
def apps_page(request: Request, db: Session = Depends(get_db)):
    user = session_user(db, request.cookies.get(SESSION_COOKIE))
    if user is None:
        return _login_redirect(request)
    rows = []
    for app in db.scalars(select(App).where(App.owner_id == user.id).order_by(App.name)):
        rev = latest_revision(db, app)
        rows.append({
            "name": app.name, "base_url": app.base_url, "updated_at": app.updated_at,
            "revision": rev.revision if rev else 0,
            "agents": db.scalar(select(func.count()).select_from(Agent).where(Agent.app_id == app.id)),
            "tools": db.scalar(select(func.count()).select_from(Tool).where(Tool.app_id == app.id)),
        })
    return _page(request, "apps.html", user=user, section="apps", apps=rows)


@router.get("/ui/apps/{app_name}", response_class=HTMLResponse)
def app_page(app_name: str, request: Request, db: Session = Depends(get_db)):
    user = session_user(db, request.cookies.get(SESSION_COOKIE))
    if user is None:
        return _login_redirect(request)
    app = user_app(db, user, app_name)  # NotFound -> 404 for other users' apps
    agents = db.scalars(select(Agent).where(Agent.app_id == app.id).order_by(Agent.name)).all()
    versions = {
        a.name: [version_view(v, a.active_version_id) for v in db.scalars(
            select(PromptVersion).where(PromptVersion.agent_id == a.id).order_by(PromptVersion.version.desc()))]
        for a in agents
    }
    grants = [{"id": g.id, "tool": f"{src}.{tool}"} for g, tool, src in db.execute(
        select(Grant, Tool.name, App.name).join(Tool, Tool.id == Grant.tool_id).join(App, App.id == Tool.app_id)
        .where(Grant.to_app_id == app.id).order_by(App.name, Tool.name))]
    rev = latest_revision(db, app)
    return _page(
        request, "app.html", user=user, section="apps", app=app, revision=rev.revision if rev else 0,
        agents=[agent_view(db, a) for a in agents], versions=versions,
        tools=[{"name": t.name, **t.spec} for t in db.scalars(select(Tool).where(Tool.app_id == app.id).order_by(Tool.name))],
        flows=[{"name": f.name, **f.spec} for f in db.scalars(select(Flow).where(Flow.app_id == app.id).order_by(Flow.name))],
        grants=grants,
        keys=[key_view(k) for k in db.scalars(select(AppKey).where(AppKey.app_id == app.id).order_by(AppKey.id.desc()))],
    )


@router.get("/ui/audit", response_class=HTMLResponse)
def audit_page(request: Request, db: Session = Depends(get_db)):
    user = session_user(db, request.cookies.get(SESSION_COOKIE))
    if user is None:
        return _login_redirect(request)
    rows = db.execute(select(AuditLog, App.name).outerjoin(App, App.id == AuditLog.app_id)
                      .where(AuditLog.user_id == user.id).order_by(AuditLog.id.desc()).limit(200)).all()
    entries = [{"created_at": a.created_at, "app": name or (a.entity_name if a.entity_type == "app" else None),
                "action": a.action, "entity_type": a.entity_type, "entity_name": a.entity_name,
                "before": a.before, "after": a.after} for a, name in rows]
    return _page(request, "audit.html", user=user, section="audit", entries=entries)
