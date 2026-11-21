"""A user's apps as SDK bundles: building, validating, revisioning, auditing.

Every management write goes through ``commit_change``. It rebuilds the
bundle of each app the write touches, compiles each with the SDK's own
config compiler (bastion.load_config), and only then stores new
bundle revisions and an audit row. If any bundle fails to compile, the
write is rejected and the request's transaction rolls back, so nothing is
saved.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

from bastion.config import ConfigError, load_config
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Agent, App, AuditLog, BundleRevision, Flow, Grant, PromptVersion, Tool, User


class ChangeRejected(Exception):
    """The change would make an app's bundle invalid. ``message`` is the compiler's."""

    def __init__(self, app: str, message: str) -> None:
        super().__init__(message)
        self.app = app
        self.message = message


class NotFound(Exception):
    pass


# ── lookups (always scoped to the owner) ──────────────────────────────────────

def user_app(db: Session, user: User, name: str) -> App:
    app = db.scalar(select(App).where(App.owner_id == user.id, App.name == name))
    if app is None:
        raise NotFound(f"App {name!r} not found.")
    return app


def child(db: Session, model, app: App, name: str):
    row = db.scalar(select(model).where(model.app_id == app.id, model.name == name))
    if row is None:
        raise NotFound(f"{model.__name__} {name!r} not found in app {app.name!r}.")
    return row


def active_version(db: Session, agent: Agent) -> PromptVersion | None:
    return db.get(PromptVersion, agent.active_version_id) if agent.active_version_id else None


# ── bundles ───────────────────────────────────────────────────────────────────

def _app_section(app: App, tools: Iterable[Tool]) -> dict[str, Any]:
    section: dict[str, Any] = {}
    if app.base_url:
        section["base_url"] = app.base_url
    if app.auth:
        section["auth"] = dict(app.auth)
    section["tools"] = {t.name: dict(t.spec) for t in sorted(tools, key=lambda t: t.name)}
    return section


def build_bundle(db: Session, app: App) -> dict[str, Any]:
    """The app's config in exactly the SDK's format (bastion.config)."""
    own_tools = db.scalars(select(Tool).where(Tool.app_id == app.id)).all()
    apps = {app.name: _app_section(app, own_tools)}

    # Tools other apps (same owner) granted to this one: include the granting
    # app's connection details and only the granted tools.
    granted = db.execute(
        select(Tool, App).join(Grant, Grant.tool_id == Tool.id).join(App, App.id == Tool.app_id)
        .where(Grant.to_app_id == app.id, App.owner_id == app.owner_id)
    ).all()
    by_source: dict[str, tuple[App, list[Tool]]] = {}
    for tool, source in granted:
        by_source.setdefault(source.name, (source, []))[1].append(tool)
    for name in sorted(by_source):
        source, tools = by_source[name]
        apps[name] = _app_section(source, tools)

    agents = {}
    for agent in sorted(db.scalars(select(Agent).where(Agent.app_id == app.id)).all(), key=lambda a: a.name):
        version = active_version(db, agent)
        agents[f"{app.name}.{agent.name}"] = {
            **dict(agent.spec),
            "instructions": version.instructions if version else "",
            "prompt_version": version.label if version else "",
        }

    flows = {
        f"{app.name}.{f.name}": dict(f.spec)
        for f in sorted(db.scalars(select(Flow).where(Flow.app_id == app.id)).all(), key=lambda f: f.name)
    }
    grants = [
        {"tool": f"{source.name}.{tool.name}", "to_apps": [app.name]}
        for source_name in sorted(by_source)
        for source, tools in [by_source[source_name]]
        for tool in sorted(tools, key=lambda t: t.name)
    ]

    bundle: dict[str, Any] = {"apps": apps, "agents": agents, "flows": flows, "grants": grants}
    if app.settings:
        bundle["settings"] = dict(app.settings)
    if app.classifier:
        bundle["classifier"] = dict(app.classifier)
    if app.dashboard:
        bundle["dashboard"] = dict(app.dashboard)
    return bundle


def etag_of(bundle: dict[str, Any]) -> str:
    canonical = json.dumps(bundle, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def compile_bundle(app_name: str, bundle: dict[str, Any]) -> None:
    try:
        load_config(bundle)
    except ConfigError as e:
        raise ChangeRejected(app_name, str(e)) from None


def latest_revision(db: Session, app: App) -> BundleRevision | None:
    return db.scalar(
        select(BundleRevision).where(BundleRevision.app_id == app.id)
        .order_by(BundleRevision.revision.desc()).limit(1)
    )


def dependents(db: Session, app: App) -> list[App]:
    """Apps whose bundle includes some of *app*'s tools through a grant."""
    # DISTINCT on ids, not whole rows: Postgres can't compare json columns.
    ids = db.scalars(
        select(Grant.to_app_id).join(Tool, Tool.id == Grant.tool_id).where(Tool.app_id == app.id).distinct()
    ).all()
    return list(db.scalars(select(App).where(App.id.in_(ids)).order_by(App.id)).all()) if ids else []


# ── the single write path ─────────────────────────────────────────────────────

def summary(row: Any) -> dict[str, Any] | None:
    """A compact, JSON-safe before/after for the audit log."""
    raw = _summary(row)
    return None if raw is None else json.loads(json.dumps(raw, default=str))


def _summary(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    if isinstance(row, App):
        return {"name": row.name, "base_url": row.base_url, "auth": row.auth,
                "settings": row.settings, "classifier": row.classifier}
    if isinstance(row, Agent):
        return {"name": row.name, "spec": row.spec, "active_version_id": row.active_version_id}
    if isinstance(row, (Tool, Flow)):
        return {"name": row.name, "spec": row.spec}
    if isinstance(row, dict):
        return row
    return {"value": str(row)}


def commit_change(
    db: Session,
    user: User | None,
    apps: Iterable[App],
    *,
    action: str,
    entity_type: str,
    entity_name: str,
    audit_app: App | None,
    before: Any = None,
    after: Any = None,
) -> dict[str, int]:
    """Validate every affected app's bundle, store new revisions for the ones
    that changed, and write the audit row. Returns {app_name: revision}.
    Raises ChangeRejected (nothing stored) if any bundle doesn't compile."""
    db.flush()
    affected: dict[int, App] = {}
    for app in apps:
        affected[app.id] = app
        for dep in dependents(db, app):
            affected[dep.id] = dep

    built = {}
    for app in affected.values():
        bundle = build_bundle(db, app)
        compile_bundle(app.name, bundle)  # raises -> whole write rejected
        built[app.id] = (app, bundle)

    revisions: dict[str, int] = {}
    for app, bundle in built.values():
        etag = etag_of(bundle)
        last = latest_revision(db, app)
        if last is not None and last.etag == etag:
            revisions[app.name] = last.revision
            continue
        number = (last.revision if last else 0) + 1
        db.add(BundleRevision(app_id=app.id, revision=number, etag=etag, bundle=bundle,
                              created_by=user.id if user else None))
        revisions[app.name] = number

    db.add(AuditLog(
        user_id=user.id if user else None, app_id=audit_app.id if audit_app else None,
        action=action, entity_type=entity_type, entity_name=entity_name,
        before=summary(before), after=summary(after),
    ))
    db.flush()
    return revisions


def next_version_number(db: Session, agent: Agent) -> int:
    return (db.scalar(select(func.max(PromptVersion.version)).where(PromptVersion.agent_id == agent.id)) or 0) + 1


def add_prompt_version(db: Session, user: User | None, agent: Agent, instructions: str,
                       label: str | None = None) -> PromptVersion:
    number = next_version_number(db, agent)
    label = label or f"v{number}"
    if db.scalar(select(PromptVersion).where(PromptVersion.agent_id == agent.id, PromptVersion.label == label)):
        label = f"v{number}"
    version = PromptVersion(agent_id=agent.id, version=number, label=label, instructions=instructions,
                            created_by=user.id if user else None)
    db.add(version)
    db.flush()
    agent.active_version_id = version.id
    return version
