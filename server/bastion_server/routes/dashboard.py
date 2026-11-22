"""Trace views (/ui): an app's overview dashboard, trace list, trace detail
and session view, plus the cross-app dashboard. Server-rendered, read-only,
always scoped to the signed-in user's apps (another user's app is a 404).

Times are UTC. Counts and sums are done in SQL; hourly and daily buckets
are made in Python from the rows of the window, which keeps the queries
the same on SQLite and Postgres.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from ..accounts import session_user
from ..deps import get_db
from ..models import Agent, App, BundleRevision, PromptVersion, Trace
from ..security import SESSION_COOKIE, aware
from ..workspace import NotFound, user_app
from .ui import _login_redirect, _page

router = APIRouter(include_in_schema=False)

PAGE_SIZE = 50
STATUSES = ("success", "blocked", "error")
WINDOW_DAYS = 7


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _counts(db: Session, where: list) -> dict[str, Any]:
    """Totals, per-status counts and tokens for the traces matching *where*."""
    row = db.execute(select(
        func.count(Trace.id),
        *[func.coalesce(func.sum(case((Trace.status == s, 1), else_=0)), 0) for s in STATUSES],
        func.coalesce(func.sum(Trace.token_input), 0), func.coalesce(func.sum(Trace.token_output), 0),
        func.sum(Trace.cost_usd), _UNPRICED,
    ).where(*where)).one()
    total, ok, blocked, error, tin, tout, cost, unpriced = row
    return {"total": total, "success": ok, "blocked": blocked, "error": error,
            "token_input": tin, "token_output": tout, "tokens": tin + tout,
            **_cost(cost, unpriced)}


# Traces with some (or all) of their cost unknown: no price for the model.
_UNPRICED = func.coalesce(func.sum(case((Trace.unpriced, 1), else_=0)), 0)


def _cost(total: float | None, unpriced: int) -> dict[str, Any]:
    """A cost total that leaves unpriced traces out and says how many there were.
    cost_usd is None (shown as "unknown") when nothing in it had a price."""
    if total is None and not unpriced:
        total = 0.0
    return {"cost_usd": total, "unpriced": int(unpriced or 0)}


def _hourly(db: Session, where: list, now: datetime) -> dict[str, Any]:
    """Traces per hour over the last 7 days, as an SVG-ready series."""
    end = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    start = end - timedelta(days=WINDOW_DAYS)
    hours = WINDOW_DAYS * 24
    total, blocked = [0] * hours, [0] * hours
    for started, status in db.execute(select(Trace.started_at, Trace.status).where(*where, Trace.started_at >= start)):
        i = int((aware(started) - start).total_seconds() // 3600)
        if 0 <= i < hours:
            total[i] += 1
            if status != "success":
                blocked[i] += 1
    return _chart(start, total, blocked)


def _chart(start: datetime, total: list[int], problems: list[int]) -> dict[str, Any]:
    width, height, pad = 960, 160, 6
    peak = max(total) or 1
    step = (width - 2 * pad) / max(len(total) - 1, 1)

    def points(series: list[int]) -> str:
        return " ".join(f"{pad + i * step:.1f},{height - pad - (v / peak) * (height - 2 * pad):.1f}"
                        for i, v in enumerate(series))

    days = [{"x": round(pad + d * 24 * step, 1), "label": (start + timedelta(days=d)).strftime("%a %d")}
            for d in range(len(total) // 24)]
    return {"width": width, "height": height, "peak": max(total), "sum": sum(total),
            "total": points(total), "problems": points(problems), "days": days}


def _filters(request: Request) -> tuple[dict[str, str], list, list[str]]:
    """Query-string filters -> (values for the form, SQL conditions, errors)."""
    q = request.query_params
    values = {k: (q.get(k) or "").strip() for k in ("agent", "status", "session", "from", "to")}
    where, errors = [], []
    if values["agent"]:
        where.append(Trace.agent_name == values["agent"])
    if values["status"]:
        if values["status"] in STATUSES:
            where.append(Trace.status == values["status"])
        else:
            errors.append(f"Unknown status {values['status']!r}.")
    if values["session"]:
        where.append(Trace.session_id == values["session"])
    for key, cmp in (("from", "ge"), ("to", "lt")):
        if not values[key]:
            continue
        try:
            day = date.fromisoformat(values[key])
        except ValueError:
            errors.append(f"{key!r} must be a date (YYYY-MM-DD).")
            continue
        at = datetime.combine(day, time.min, tzinfo=timezone.utc)
        where.append(Trace.started_at >= at if cmp == "ge" else Trace.started_at < at + timedelta(days=1))
    return values, where, errors


def _row(t: Trace, app_name: str | None = None) -> dict[str, Any]:
    return {"trace_id": t.trace_id, "run_id": t.run_id, "app": app_name, "agent": t.agent_name,
            "flow": t.flow_name, "session_id": t.session_id, "status": t.status, "model": t.model,
            "tokens": t.token_input + t.token_output, "token_input": t.token_input,
            "token_output": t.token_output, "duration_ms": t.duration_ms,
            "started_at": aware(t.started_at), "cost_usd": t.cost_usd, "unpriced": t.unpriced,
            "unpriced_models": (t.summary or {}).get("unpriced_models") or [],
            "violations": len((t.summary or {}).get("violations") or [])}


def _signed_in(request: Request, db: Session):
    return session_user(db, request.cookies.get(SESSION_COOKIE))


# ── per app ───────────────────────────────────────────────────────────────────

@router.get("/ui/apps/{app_name}/dashboard", response_class=HTMLResponse)
def app_dashboard(app_name: str, request: Request, db: Session = Depends(get_db)):
    user = _signed_in(request, db)
    if user is None:
        return _login_redirect(request)
    app = user_app(db, user, app_name)
    now = _now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week = now - timedelta(days=WINDOW_DAYS)
    mine = [Trace.app_id == app.id]

    top = db.execute(
        select(Trace.agent_name, func.count(Trace.id), func.avg(Trace.duration_ms),
               func.sum(Trace.token_input + Trace.token_output))
        .where(*mine, Trace.started_at >= week).group_by(Trace.agent_name).order_by(func.count(Trace.id).desc())
    ).all()
    blocked = db.scalars(select(Trace).where(*mine, Trace.status == "blocked")
                         .order_by(Trace.started_at.desc(), Trace.id.desc()).limit(10)).all()
    recent = db.scalars(select(Trace).where(*mine).order_by(Trace.started_at.desc(), Trace.id.desc()).limit(10)).all()
    return _page(
        request, "dashboard.html", user=user, section="apps", tab="dashboard", app=app,
        today=_counts(db, [*mine, Trace.started_at >= today]), week=_counts(db, [*mine, Trace.started_at >= week]),
        chart=_hourly(db, mine, now),
        agents=[{"agent": a, "count": n, "avg_ms": round(avg or 0, 1), "tokens": tok or 0} for a, n, avg, tok in top],
        blocked=[_row(t) for t in blocked], recent=[_row(t) for t in recent],
        content_on=bool((app.dashboard or {}).get("include_content")),
    )


@router.get("/ui/apps/{app_name}/traces", response_class=HTMLResponse)
def traces_page(app_name: str, request: Request, db: Session = Depends(get_db)):
    user = _signed_in(request, db)
    if user is None:
        return _login_redirect(request)
    app = user_app(db, user, app_name)
    values, where, errors = _filters(request)
    where = [Trace.app_id == app.id, *where]
    try:
        page = max(1, int(request.query_params.get("page") or 1))
    except ValueError:
        page = 1
    total = db.scalar(select(func.count(Trace.id)).where(*where)) or 0
    pages = max(1, -(-total // PAGE_SIZE))
    page = min(page, pages)
    rows = db.scalars(select(Trace).where(*where).order_by(Trace.started_at.desc(), Trace.id.desc())
                      .offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)).all()
    agent_names = sorted(set(db.scalars(select(Trace.agent_name).where(Trace.app_id == app.id).distinct()))
                         | set(db.scalars(select(Agent.name).where(Agent.app_id == app.id))))
    keep = {k: v for k, v in values.items() if v}

    def page_url(n: int) -> str:
        return f"?{urlencode({**keep, 'page': n})}"

    return _page(
        request, "traces.html", user=user, section="apps", tab="traces", app=app, rows=[_row(t) for t in rows],
        filters=values, errors=errors, statuses=STATUSES, agent_names=agent_names,
        total=total, page=page, pages=pages,
        prev_url=page_url(page - 1) if page > 1 else None, next_url=page_url(page + 1) if page < pages else None,
    )


def _prompt_link(db: Session, app: App, t: Trace) -> dict[str, Any] | None:
    """The prompt version this run used: from its bundle revision when the
    service still has it, else the label the SDK reported."""
    label = t.prompt_version
    if t.bundle_revision is not None:
        rev = db.scalar(select(BundleRevision).where(BundleRevision.app_id == app.id,
                                                     BundleRevision.revision == t.bundle_revision))
        if rev is not None:
            label = ((rev.bundle.get("agents") or {}).get(f"{app.name}.{t.agent_name}") or {}).get("prompt_version") or label
    if not label:
        return None
    version = None
    if t.agent_id is not None:
        version = db.scalar(select(PromptVersion).where(PromptVersion.agent_id == t.agent_id, PromptVersion.label == label))
    return {"label": label, "exists": version is not None,
            "href": f"/ui/apps/{app.name}#v-{t.agent_name}-{label}" if version is not None else None}


def _timeline(t: Trace) -> dict[str, Any]:
    steps = sorted(t.steps or [], key=lambda s: s.get("offset_ms") or 0)

    def ends_at(s: dict) -> bool:
        # agent_end and tool_result are stamped when the work finished; their
        # duration reaches back from there.
        return s.get("type") in ("agent_end", "tool_result")

    span = max([t.duration_ms or 0] + [(s.get("offset_ms") or 0) + (0 if ends_at(s) else s.get("duration_ms") or 0)
                                       for s in steps]) or 1
    out = []
    for s in steps:
        offset, duration = s.get("offset_ms") or 0, s.get("duration_ms")
        bar_start = offset - duration if ends_at(s) and duration else offset
        out.append({**s, "left": round(100 * max(bar_start, 0) / span, 2),
                    "width": round(max(100 * (duration or 0) / span, 0.6), 2), "has_duration": duration is not None})
    return {"span_ms": round(span, 1), "steps": out}


def _detail(db: Session, app: App, t: Trace) -> dict[str, Any]:
    summary = t.summary or {}
    return {
        **_row(t), "bundle_revision": t.bundle_revision, "ended_at": aware(t.ended_at),
        "kind": summary.get("kind", "agent"), "prompt": _prompt_link(db, app, t),
        "timeline": _timeline(t), "tool_calls": [
            {**c, "cross_app": bool(c.get("app")) and c.get("app") != app.name} for c in t.tool_calls or []],
        "agents": summary.get("agents") or [], "violations": summary.get("violations") or [],
        "scans": summary.get("scans") or [], "hook_denials": summary.get("hook_denials") or 0,
        "content": t.content, "received_at": aware(t.received_at),
    }


@router.get("/ui/apps/{app_name}/traces/{trace_id}", response_class=HTMLResponse)
def trace_page(app_name: str, trace_id: str, request: Request, db: Session = Depends(get_db)):
    user = _signed_in(request, db)
    if user is None:
        return _login_redirect(request)
    app = user_app(db, user, app_name)
    # Callers may reuse one trace_id for several runs; each run is one row.
    runs = db.scalars(select(Trace).where(Trace.app_id == app.id, Trace.trace_id == trace_id)
                      .order_by(Trace.started_at, Trace.id)).all()
    if not runs:
        raise NotFound(f"No trace {trace_id!r} in {app_name!r}.")
    wanted = request.query_params.get("run")
    t = next((r for r in runs if r.run_id == wanted), runs[0])
    return _page(request, "trace.html", user=user, section="apps", tab="traces", app=app,
                 t=_detail(db, app, t), runs=[{"run_id": r.run_id, "started_at": aware(r.started_at),
                                              "agent": r.agent_name, "status": r.status} for r in runs])


@router.get("/ui/apps/{app_name}/sessions/{session_id}", response_class=HTMLResponse)
def session_page(app_name: str, session_id: str, request: Request, db: Session = Depends(get_db)):
    user = _signed_in(request, db)
    if user is None:
        return _login_redirect(request)
    app = user_app(db, user, app_name)
    traces = db.scalars(select(Trace).where(Trace.app_id == app.id, Trace.session_id == session_id)
                        .order_by(Trace.started_at, Trace.id)).all()
    if not traces:
        raise NotFound(f"No session {session_id!r} in {app_name!r}.")
    turns = [{**_row(t), "content": t.content, "tool_calls": t.tool_calls or [],
              "violations": (t.summary or {}).get("violations") or []} for t in traces]
    return _page(request, "session.html", user=user, section="apps", tab="traces", app=app,
                 session_id=session_id, turns=turns, totals=_counts(db, [Trace.app_id == app.id,
                                                                         Trace.session_id == session_id]))


# ── across apps ───────────────────────────────────────────────────────────────

WINDOWS = (7, 30, 90)


def _daily_bars(by_day: dict[date, int], start: date, days: int) -> dict[str, Any]:
    width, height = 960, 120
    values = [by_day.get(start + timedelta(days=i), 0) for i in range(days)]
    peak = max(values) or 1
    slot = width / days
    bars = [{"x": round(i * slot + slot * 0.15, 1), "w": round(slot * 0.7, 1),
             "h": round((v / peak) * (height - 4), 1), "y": round(height - (v / peak) * (height - 4), 1),
             "value": v, "day": (start + timedelta(days=i)).isoformat()} for i, v in enumerate(values)]
    every = max(1, days // 8)
    labels = [{"x": b["x"], "label": (start + timedelta(days=i)).strftime("%b %d")}
              for i, b in enumerate(bars) if i % every == 0]
    return {"width": width, "height": height, "bars": bars, "labels": labels, "peak": max(values)}


@router.get("/ui/dashboard", response_class=HTMLResponse)
def all_apps_dashboard(request: Request, db: Session = Depends(get_db)):
    user = _signed_in(request, db)
    if user is None:
        return _login_redirect(request)
    try:
        days = int(request.query_params.get("days") or 30)
    except ValueError:
        days = 30
    days = days if days in WINDOWS else 30
    now = _now()
    first_day = (now - timedelta(days=days - 1)).date()
    since = datetime.combine(first_day, time.min, tzinfo=timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    apps = db.scalars(select(App).where(App.owner_id == user.id).order_by(App.name)).all()
    names = {a.id: a.name for a in apps}
    ids = list(names) or [-1]
    mine = [Trace.app_id.in_(ids)]
    window = [*mine, Trace.started_at >= since]

    tokens = Trace.token_input + Trace.token_output
    per_app = {app_id: {"traces": n, "blocked": b, "errors": e, "tokens": tok or 0, **_cost(cost, unpriced),
                        "last": aware(last)}
               for app_id, n, b, e, tok, cost, unpriced, last in db.execute(
                   select(Trace.app_id, func.count(Trace.id),
                          func.coalesce(func.sum(case((Trace.status == "blocked", 1), else_=0)), 0),
                          func.coalesce(func.sum(case((Trace.status == "error", 1), else_=0)), 0),
                          func.sum(tokens), func.sum(Trace.cost_usd), _UNPRICED, func.max(Trace.started_at))
                   .where(*window).group_by(Trace.app_id))}
    app_rows = [{"name": a.name, **per_app.get(a.id, {"traces": 0, "blocked": 0, "errors": 0, "tokens": 0,
                                                       **_cost(None, 0), "last": None})} for a in apps]

    by_model = [{"model": m or "unknown", "traces": n, "token_input": i or 0, "token_output": o or 0,
                 "tokens": (i or 0) + (o or 0), **_cost(c, unpriced)}
                for m, n, i, o, c, unpriced in db.execute(
                    select(Trace.model, func.count(Trace.id), func.sum(Trace.token_input),
                           func.sum(Trace.token_output), func.sum(Trace.cost_usd), _UNPRICED)
                    .where(*window).group_by(Trace.model).order_by(func.sum(tokens).desc()))]

    by_day: dict[date, dict[str, Any]] = {}
    for started, app_id, tok in db.execute(select(Trace.started_at, Trace.app_id, tokens).where(*window)):
        d = aware(started).date()
        row = by_day.setdefault(d, {"tokens": 0, "traces": 0, "apps": {}})
        row["tokens"] += tok or 0
        row["traces"] += 1
        row["apps"][names[app_id]] = row["apps"].get(names[app_id], 0) + (tok or 0)
    day_rows = [{"day": d, **by_day[d]} for d in sorted(by_day, reverse=True)]

    recent = db.execute(select(Trace, App.name).join(App, App.id == Trace.app_id).where(*mine)
                        .order_by(Trace.started_at.desc(), Trace.id.desc()).limit(20)).all()
    return _page(
        request, "overview.html", user=user, section="dashboard", days=days, windows=WINDOWS,
        totals=_counts(db, window), today=_counts(db, [*mine, Trace.started_at >= today]),
        apps=app_rows, by_model=by_model, by_day=day_rows,
        bars=_daily_bars({d: r["tokens"] for d, r in by_day.items()}, first_day, days),
        recent=[_row(t, name) for t, name in recent],
    )
