"""Import and export bastion YAML (/v1/apps/{app}/import, /export)."""

from __future__ import annotations

from typing import Any

import yaml
from bastion.config import ConfigError, load_config
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import ValidationError
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..deps import csrf, current_user, get_db
from ..models import Agent, App, Flow, Grant, Tool, User
from ..workspace import (
    NotFound, active_version, add_prompt_version, build_bundle, child, commit_change, latest_revision,
    summary, user_app,
)
from .manage import DashboardIn, AgentSpec, AppIn, AuthIn, FlowSpec, ToolSpec, _dump

router = APIRouter(prefix="/v1/apps/{app_name}", tags=["import-export"], dependencies=[Depends(csrf)])

MAX_IMPORT_BYTES = 1_000_000


def _parse(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_IMPORT_BYTES:
        raise HTTPException(413, "Config is larger than 1 MB.")
    try:
        doc = yaml.safe_load(raw) or {}
    except yaml.YAMLError as e:
        raise HTTPException(422, f"Not valid YAML: {e}") from None
    if not isinstance(doc, dict):
        raise HTTPException(422, "The config must be a YAML mapping.")
    try:
        load_config(doc)  # must stand on its own as an bastion config
    except ConfigError as e:
        raise HTTPException(422, str(e)) from None
    return doc


def _strict(model, data: dict, what: str, authored: bool = False) -> dict:
    """Validate with the API's strict model (no unknown fields, so no secrets)."""
    try:
        obj = model(**data)
        return obj.model_dump(exclude_unset=True) if authored else _dump(obj)
    except ValidationError as e:
        raise HTTPException(422, f"{what}: {e.errors()[0]['msg']} ({'.'.join(map(str, e.errors()[0]['loc']))})") from None


@router.post("/import")
async def import_config(app_name: str, request: Request, db: Session = Depends(get_db),
                        user: User = Depends(current_user)):
    """Replace this app's definition with the YAML's. Body: the YAML text."""
    doc = _parse(await request.body())
    apps = doc.get("apps") or {}
    if app_name not in apps:
        raise HTTPException(422, f"The config has no app named {app_name!r} (it has: {sorted(apps)}).")
    AppIn(name=app_name)  # same name rules as creating an app

    app = db.scalar(select(App).where(App.owner_id == user.id, App.name == app_name))
    created = app is None
    if created:
        app = App(owner_id=user.id, name=app_name, settings={}, classifier={})
        db.add(app)
        db.flush()
    before = None if created else summary(build_bundle(db, app))

    section = apps[app_name] or {}
    app.base_url = section.get("base_url")
    app.auth = _strict(AuthIn, section["auth"], "auth") if section.get("auth") else None
    app.settings = dict(doc.get("settings") or {})
    app.classifier = dict(doc.get("classifier") or {})
    dash = dict(doc.get("dashboard") or {})
    if dash.get("key"):
        raise HTTPException(422, "dashboard.key: the service never stores app keys. Remove it from the "
                                 "file and give the app its key through BASTION_KEY.")
    dash.pop("key", None)
    dash.pop("url", None)  # the app's own setting (BASTION_URL), not part of its bundle
    for local_only in ("flush_interval", "batch_size"):
        dash.pop(local_only, None)
    app.dashboard = _strict(DashboardIn, dash, "dashboard") if dash else {}

    # tools: replace
    tools_in = section.get("tools") or {}
    specs = {name: _strict(ToolSpec, spec or {}, f"tool {name!r}", authored=True) for name, spec in tools_in.items()}
    existing = {t.name: t for t in db.scalars(select(Tool).where(Tool.app_id == app.id))}
    for name, tool in existing.items():
        if name not in specs:
            db.delete(tool)
    for name, spec in specs.items():
        if name in existing:
            existing[name].spec = spec
        else:
            db.add(Tool(app_id=app.id, name=name, spec=spec))
    db.flush()

    # agents: replace definitions; prompts become versions (history kept)
    prefix = f"{app_name}."
    agents_in = {k[len(prefix):]: v for k, v in (doc.get("agents") or {}).items() if k.startswith(prefix)}
    skipped = sorted(k for k in (doc.get("agents") or {}) if not k.startswith(prefix))
    current = {a.name: a for a in db.scalars(select(Agent).where(Agent.app_id == app.id))}
    for name, agent in current.items():
        if name not in agents_in:
            agent.active_version_id = None
            db.flush()
            db.delete(agent)
    for name, data in agents_in.items():
        data = dict(data)
        instructions, label = data.pop("instructions"), data.pop("prompt_version", None)
        spec = _strict(AgentSpec, data, f"agent {name!r}")
        agent = current.get(name)
        if agent is None:
            agent = Agent(app_id=app.id, name=name, spec=spec)
            db.add(agent)
            db.flush()
        else:
            agent.spec = spec
        active = active_version(db, agent)
        if active is None or active.instructions != instructions:
            add_prompt_version(db, user, agent, instructions, label=str(label) if label else None)
    db.flush()

    # flows: replace
    db.execute(delete(Flow).where(Flow.app_id == app.id))
    for key, spec in (doc.get("flows") or {}).items():
        if key.startswith(prefix):
            db.add(Flow(app_id=app.id, name=key[len(prefix):], spec=_strict(FlowSpec, spec, f"flow {key!r}")))

    # grants to this app: replace (the granting app must already be yours)
    db.execute(delete(Grant).where(Grant.to_app_id == app.id))
    for g in doc.get("grants") or []:
        if app_name not in (g.get("to_apps") or []) or not g.get("tool"):
            continue
        source_name, _, tool_name = g["tool"].partition(".")
        try:
            tool = child(db, Tool, user_app(db, user, source_name), tool_name)
        except NotFound:
            raise HTTPException(422, f"Grant of {g['tool']!r}: create app {source_name!r} with tool "
                                     f"{tool_name!r} first, then import again.") from None
        db.add(Grant(tool_id=tool.id, to_app_id=app.id))

    revisions = commit_change(db, user, [app], action="import", entity_type="app", entity_name=app_name,
                              audit_app=app, before=before, after=build_bundle(db, app))
    return {"app": app_name, "created": created, "revision": revisions.get(app_name),
            "skipped_agents": skipped}


@router.get("/export")
def export_config(app_name: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    app = user_app(db, user, app_name)
    rev = latest_revision(db, app)
    bundle = rev.bundle if rev else build_bundle(db, app)
    text = f"# bastion config for app {app_name!r}, revision {rev.revision if rev else 0}\n"
    text += yaml.safe_dump(bundle, sort_keys=False, allow_unicode=True, width=100)
    return Response(text, media_type="application/yaml",
                    headers={"Content-Disposition": f'attachment; filename="{app_name}.bastion.yaml"'})
