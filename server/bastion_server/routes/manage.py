"""Management API (/v1): apps, tools, agents and prompt versions, flows,
grants, bundles and the audit log. Session-cookie auth plus the CSRF header
on writes; everything is scoped to the signed-in user (another user's app
is a 404)."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..deps import csrf, current_user, get_db
from ..models import Agent, App, AuditLog, BundleRevision, Flow, Grant, PromptVersion, Tool, User
from ..workspace import (
    NotFound, active_version, add_prompt_version, build_bundle, child, commit_change,
    dependents, etag_of, latest_revision, user_app,
)

router = APIRouter(prefix="/v1", tags=["manage"], dependencies=[Depends(csrf)])

_NAME = r"^[A-Za-z][A-Za-z0-9_-]{0,62}$"
_ENV = r"^[A-Z_][A-Z0-9_]{0,127}$"
_HEADER = r"^[A-Za-z0-9-]{1,100}$"


def _no_double_underscore(v: str) -> str:
    if "__" in v:
        raise ValueError("names can't contain '__' (reserved for cross-app tool names)")
    return v


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AuthIn(Strict):
    """How tools authenticate. Only names: the secret stays in the app's environment."""
    header: str = Field(pattern=_HEADER)
    secret_env: str = Field(pattern=_ENV, description="Name of the env var holding the secret")
    prefix: str = Field(default="", max_length=50)


_CREDENTIAL = r"^env:[A-Z_][A-Z0-9_]{0,127}$"


def _credentials(value: dict[str, str] | None) -> dict[str, str] | None:
    """{granting app: "env:VAR"}: a name pattern on both sides, never a secret."""
    import re

    for app, ref in (value or {}).items():
        if not re.match(_NAME, app) or not isinstance(ref, str) or not re.match(_CREDENTIAL, ref):
            raise ValueError(f"credentials must map app names to 'env:VAR' (got {app!r}: {ref!r})")
    return value


class DashboardIn(Strict):
    """What the SDK's DashboardSink takes from the bundle. The URL and key
    are the app's own (BASTION_URL / BASTION_KEY), never stored here."""

    include_content: bool = False


class AppIn(Strict):
    name: str = Field(pattern=_NAME)
    base_url: str | None = Field(default=None, max_length=2048)
    auth: AuthIn | None = None
    credentials: dict[str, str] = Field(default_factory=dict)
    settings: dict[str, Any] = Field(default_factory=dict)
    classifier: dict[str, Any] = Field(default_factory=dict)
    dashboard: DashboardIn | None = None
    _n = field_validator("name")(_no_double_underscore)
    _c = field_validator("credentials")(_credentials)


class AppPatch(Strict):
    base_url: str | None = None
    auth: AuthIn | None = None
    credentials: dict[str, str] | None = None
    _c = field_validator("credentials")(_credentials)
    settings: dict[str, Any] | None = None
    classifier: dict[str, Any] | None = None
    dashboard: DashboardIn | None = None


class ToolSpec(Strict):
    """Anvil tool fields (no name; it's in the URL or body)."""
    path: str | None = None
    endpoint: str | None = None
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "POST"
    description: str = ""
    trust: Literal["trusted", "untrusted"] = "untrusted"
    timeout: float = 10.0
    injection_scanning: bool = True
    parameters: dict[str, Any] | None = None
    response_schema: dict[str, Any] | None = None
    redact_fields: list[str] = Field(default_factory=list)


class ToolIn(ToolSpec):
    name: str = Field(pattern=_NAME)
    _n = field_validator("name")(_no_double_underscore)


class AgentSpec(Strict):
    backend: Literal["openai", "anthropic"] = "openai"
    model: str | None = None
    role: str = ""
    description: str = ""
    tools: list[str] = Field(default_factory=list)
    sequences: list[dict[str, Any]] = Field(default_factory=list)
    strictness: Literal["strict", "loose"] = "strict"
    trust_overrides: dict[str, Literal["trusted", "untrusted"]] = Field(default_factory=dict)


class AgentIn(AgentSpec):
    name: str = Field(pattern=_NAME)
    instructions: str = Field(min_length=1)
    _n = field_validator("name")(_no_double_underscore)


class AgentPatch(Strict):
    """Any subset. New instructions create a new immutable prompt version."""
    instructions: str | None = Field(default=None, min_length=1)
    backend: Literal["openai", "anthropic"] | None = None
    model: str | None = None
    role: str | None = None
    description: str | None = None
    tools: list[str] | None = None
    sequences: list[dict[str, Any]] | None = None
    strictness: Literal["strict", "loose"] | None = None
    trust_overrides: dict[str, Literal["trusted", "untrusted"]] | None = None


class FlowSpec(Strict):
    entry: str
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]] = Field(default_factory=list)


class FlowIn(FlowSpec):
    name: str = Field(pattern=_NAME)
    _n = field_validator("name")(_no_double_underscore)


class GrantIn(Strict):
    tool: str = Field(description="The granting app's tool, as 'app.tool' (an app you own)")


class Rollback(Strict):
    version: int = Field(ge=1)


# ── views ──────────────────────────────────────────────────────────────────

def _dump(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(exclude_none=True, exclude_defaults=False)


def _authored(model: BaseModel, exclude: set[str] | None = None) -> dict[str, Any]:
    """Only the fields that were actually given, so a stored tool is exactly
    what was authored (no defaults added) and round-trips byte-identical."""
    return model.model_dump(exclude_unset=True, exclude=exclude or set())


def app_view(db: Session, app: App) -> dict[str, Any]:
    rev = latest_revision(db, app)
    return {
        "name": app.name, "base_url": app.base_url, "auth": app.auth, "credentials": app.credentials or {},
        "settings": app.settings, "classifier": app.classifier, "dashboard": app.dashboard or {},
        "revision": rev.revision if rev else 0,
        "created_at": app.created_at, "updated_at": app.updated_at,
    }


def agent_view(db: Session, agent: Agent) -> dict[str, Any]:
    v = active_version(db, agent)
    return {"name": agent.name, **agent.spec,
            "instructions": v.instructions if v else "", "prompt_version": v.label if v else None,
            "active_version": v.version if v else None}


def version_view(v: PromptVersion, active_id: int | None) -> dict[str, Any]:
    return {"version": v.version, "label": v.label, "instructions": v.instructions,
            "active": v.id == active_id, "created_at": v.created_at}


# ── apps ───────────────────────────────────────────────────────────────────

@router.get("/apps")
def list_apps(db: Session = Depends(get_db), user: User = Depends(current_user)):
    apps = db.scalars(select(App).where(App.owner_id == user.id).order_by(App.name)).all()
    return [app_view(db, a) for a in apps]


@router.post("/apps", status_code=201)
def create_app_(body: AppIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    if db.scalar(select(App).where(App.owner_id == user.id, App.name == body.name)):
        raise HTTPException(409, f"You already have an app named {body.name!r}.")
    app = App(owner_id=user.id, name=body.name, base_url=body.base_url,
              auth=_dump(body.auth) if body.auth else None, credentials=dict(body.credentials),
              settings=body.settings, classifier=body.classifier,
              dashboard=_dump(body.dashboard) if body.dashboard else {})
    db.add(app)
    db.flush()
    commit_change(db, user, [app], action="create", entity_type="app", entity_name=app.name,
                  audit_app=app, after=app)
    return app_view(db, app)


@router.get("/apps/{app_name}")
def get_app(app_name: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return app_view(db, user_app(db, user, app_name))


@router.patch("/apps/{app_name}")
def patch_app(app_name: str, body: AppPatch, db: Session = Depends(get_db), user: User = Depends(current_user)):
    app = user_app(db, user, app_name)
    before = dict(app_view(db, app))
    fields = body.model_fields_set
    if "base_url" in fields:
        app.base_url = body.base_url
    if "auth" in fields:
        app.auth = _dump(body.auth) if body.auth else None
    if "credentials" in fields:
        app.credentials = dict(body.credentials or {})
    if "settings" in fields:
        app.settings = body.settings or {}
    if "classifier" in fields:
        app.classifier = body.classifier or {}
    if "dashboard" in fields:
        app.dashboard = _dump(body.dashboard) if body.dashboard else {}
    commit_change(db, user, [app], action="update", entity_type="app", entity_name=app.name,
                  audit_app=app, before=before, after=app)
    return app_view(db, app)


@router.delete("/apps/{app_name}")
def delete_app(app_name: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    app = user_app(db, user, app_name)
    deps = [d for d in dependents(db, app) if d.id != app.id]
    before = app_view(db, app)
    db.delete(app)
    db.flush()
    # Apps that used this app's tools through grants must still compile without them.
    commit_change(db, user, deps, action="delete", entity_type="app", entity_name=app_name,
                  audit_app=None, before=before)
    return {"deleted": app_name}


@router.get("/apps/{app_name}/bundle")
def get_bundle(app_name: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    app = user_app(db, user, app_name)
    rev = latest_revision(db, app)
    bundle = rev.bundle if rev else build_bundle(db, app)
    return {"app": app.name, "revision": rev.revision if rev else 0, "etag": etag_of(bundle), "bundle": bundle}


@router.get("/apps/{app_name}/revisions")
def list_revisions(app_name: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    app = user_app(db, user, app_name)
    revs = db.scalars(select(BundleRevision).where(BundleRevision.app_id == app.id)
                      .order_by(BundleRevision.revision.desc())).all()
    return [{"revision": r.revision, "etag": r.etag, "created_at": r.created_at} for r in revs]


# ── tools ──────────────────────────────────────────────────────────────────

@router.get("/apps/{app_name}/tools")
def list_tools(app_name: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    app = user_app(db, user, app_name)
    return [{"name": t.name, **t.spec} for t in db.scalars(select(Tool).where(Tool.app_id == app.id).order_by(Tool.name))]


@router.post("/apps/{app_name}/tools", status_code=201)
def create_tool(app_name: str, body: ToolIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    app = user_app(db, user, app_name)
    if db.scalar(select(Tool).where(Tool.app_id == app.id, Tool.name == body.name)):
        raise HTTPException(409, f"Tool {body.name!r} already exists in {app.name!r}.")
    tool = Tool(app_id=app.id, name=body.name, spec=_authored(body, {"name"}))
    db.add(tool)
    commit_change(db, user, [app], action="create", entity_type="tool", entity_name=f"{app.name}.{tool.name}",
                  audit_app=app, after=tool)
    return {"name": tool.name, **tool.spec}


@router.get("/apps/{app_name}/tools/{tool_name}")
def get_tool(app_name: str, tool_name: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    tool = child(db, Tool, user_app(db, user, app_name), tool_name)
    return {"name": tool.name, **tool.spec}


@router.put("/apps/{app_name}/tools/{tool_name}")
def replace_tool(app_name: str, tool_name: str, body: ToolSpec, db: Session = Depends(get_db),
                 user: User = Depends(current_user)):
    app = user_app(db, user, app_name)
    tool = child(db, Tool, app, tool_name)
    before = {"name": tool.name, "spec": dict(tool.spec)}
    tool.spec = _authored(body)
    commit_change(db, user, [app], action="update", entity_type="tool", entity_name=f"{app.name}.{tool.name}",
                  audit_app=app, before=before, after=tool)
    return {"name": tool.name, **tool.spec}


@router.delete("/apps/{app_name}/tools/{tool_name}")
def delete_tool(app_name: str, tool_name: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    app = user_app(db, user, app_name)
    tool = child(db, Tool, app, tool_name)
    deps = dependents(db, app)  # before the grants cascade away
    before = {"name": tool.name, "spec": dict(tool.spec)}
    db.delete(tool)
    commit_change(db, user, [app, *deps], action="delete", entity_type="tool",
                  entity_name=f"{app.name}.{tool_name}", audit_app=app, before=before)
    return {"deleted": tool_name}


# ── agents and prompt versions ─────────────────────────────────────────────

@router.get("/apps/{app_name}/agents")
def list_agents(app_name: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    app = user_app(db, user, app_name)
    return [agent_view(db, a) for a in db.scalars(select(Agent).where(Agent.app_id == app.id).order_by(Agent.name))]


@router.post("/apps/{app_name}/agents", status_code=201)
def create_agent(app_name: str, body: AgentIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    app = user_app(db, user, app_name)
    if db.scalar(select(Agent).where(Agent.app_id == app.id, Agent.name == body.name)):
        raise HTTPException(409, f"Agent {body.name!r} already exists in {app.name!r}.")
    agent = Agent(app_id=app.id, name=body.name,
                  spec=_dump(AgentSpec(**body.model_dump(exclude={"name", "instructions"}))))
    db.add(agent)
    db.flush()
    add_prompt_version(db, user, agent, body.instructions)
    commit_change(db, user, [app], action="create", entity_type="agent", entity_name=f"{app.name}.{agent.name}",
                  audit_app=app, after=agent_view(db, agent))
    return agent_view(db, agent)


@router.get("/apps/{app_name}/agents/{agent_name}")
def get_agent(app_name: str, agent_name: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return agent_view(db, child(db, Agent, user_app(db, user, app_name), agent_name))


@router.patch("/apps/{app_name}/agents/{agent_name}")
def patch_agent(app_name: str, agent_name: str, body: AgentPatch, db: Session = Depends(get_db),
                user: User = Depends(current_user)):
    app = user_app(db, user, app_name)
    agent = child(db, Agent, app, agent_name)
    before = agent_view(db, agent)
    spec_changes = body.model_dump(exclude={"instructions"}, exclude_unset=True)
    if spec_changes:
        agent.spec = _dump(AgentSpec(**{**agent.spec, **spec_changes}))
    current = active_version(db, agent)
    if body.instructions is not None and (current is None or body.instructions != current.instructions):
        add_prompt_version(db, user, agent, body.instructions)
    commit_change(db, user, [app], action="update", entity_type="agent", entity_name=f"{app.name}.{agent.name}",
                  audit_app=app, before=before, after=agent_view(db, agent))
    return agent_view(db, agent)


@router.delete("/apps/{app_name}/agents/{agent_name}")
def delete_agent(app_name: str, agent_name: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    app = user_app(db, user, app_name)
    agent = child(db, Agent, app, agent_name)
    before = agent_view(db, agent)
    agent.active_version_id = None
    db.flush()
    db.delete(agent)
    commit_change(db, user, [app], action="delete", entity_type="agent", entity_name=f"{app.name}.{agent_name}",
                  audit_app=app, before=before)
    return {"deleted": agent_name}


@router.get("/apps/{app_name}/agents/{agent_name}/versions")
def list_versions(app_name: str, agent_name: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    agent = child(db, Agent, user_app(db, user, app_name), agent_name)
    versions = db.scalars(select(PromptVersion).where(PromptVersion.agent_id == agent.id)
                          .order_by(PromptVersion.version.desc())).all()
    return [version_view(v, agent.active_version_id) for v in versions]


@router.post("/apps/{app_name}/agents/{agent_name}/rollback")
def rollback(app_name: str, agent_name: str, body: Rollback, db: Session = Depends(get_db),
             user: User = Depends(current_user)):
    """Point the agent at an existing version. Versions themselves never change."""
    app = user_app(db, user, app_name)
    agent = child(db, Agent, app, agent_name)
    target = db.scalar(select(PromptVersion).where(PromptVersion.agent_id == agent.id,
                                                    PromptVersion.version == body.version))
    if target is None:
        raise NotFound(f"Agent {agent_name!r} has no version {body.version}.")
    before = agent_view(db, agent)
    agent.active_version_id = target.id
    commit_change(db, user, [app], action="rollback", entity_type="agent", entity_name=f"{app.name}.{agent.name}",
                  audit_app=app, before=before, after=agent_view(db, agent))
    return agent_view(db, agent)


# ── flows ──────────────────────────────────────────────────────────────────

@router.get("/apps/{app_name}/flows")
def list_flows(app_name: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    app = user_app(db, user, app_name)
    return [{"name": f.name, **f.spec} for f in db.scalars(select(Flow).where(Flow.app_id == app.id).order_by(Flow.name))]


@router.post("/apps/{app_name}/flows", status_code=201)
def create_flow(app_name: str, body: FlowIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    app = user_app(db, user, app_name)
    if db.scalar(select(Flow).where(Flow.app_id == app.id, Flow.name == body.name)):
        raise HTTPException(409, f"Flow {body.name!r} already exists in {app.name!r}.")
    flow = Flow(app_id=app.id, name=body.name, spec=_dump(FlowSpec(**body.model_dump(exclude={"name"}))))
    db.add(flow)
    commit_change(db, user, [app], action="create", entity_type="flow", entity_name=f"{app.name}.{flow.name}",
                  audit_app=app, after=flow)
    return {"name": flow.name, **flow.spec}


@router.put("/apps/{app_name}/flows/{flow_name}")
def replace_flow(app_name: str, flow_name: str, body: FlowSpec, db: Session = Depends(get_db),
                 user: User = Depends(current_user)):
    app = user_app(db, user, app_name)
    flow = child(db, Flow, app, flow_name)
    before = {"name": flow.name, "spec": dict(flow.spec)}
    flow.spec = _dump(body)
    commit_change(db, user, [app], action="update", entity_type="flow", entity_name=f"{app.name}.{flow.name}",
                  audit_app=app, before=before, after=flow)
    return {"name": flow.name, **flow.spec}


@router.delete("/apps/{app_name}/flows/{flow_name}")
def delete_flow(app_name: str, flow_name: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    app = user_app(db, user, app_name)
    flow = child(db, Flow, app, flow_name)
    before = {"name": flow.name, "spec": dict(flow.spec)}
    db.delete(flow)
    commit_change(db, user, [app], action="delete", entity_type="flow", entity_name=f"{app.name}.{flow_name}",
                  audit_app=app, before=before)
    return {"deleted": flow_name}


# ── grants (tools other apps of yours give to this one) ────────────────────

def _grant_view(grant: Grant, tool: Tool, source: App) -> dict[str, Any]:
    return {"id": grant.id, "tool": f"{source.name}.{tool.name}", "created_at": grant.created_at}


@router.get("/apps/{app_name}/grants")
def list_grants(app_name: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    app = user_app(db, user, app_name)
    rows = db.execute(select(Grant, Tool, App).join(Tool, Tool.id == Grant.tool_id).join(App, App.id == Tool.app_id)
                      .where(Grant.to_app_id == app.id).order_by(App.name, Tool.name)).all()
    return [_grant_view(g, t, s) for g, t, s in rows]


@router.post("/apps/{app_name}/grants", status_code=201)
def create_grant(app_name: str, body: GrantIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    app = user_app(db, user, app_name)
    source_name, _, tool_name = body.tool.partition(".")
    if not tool_name:
        raise HTTPException(422, "Name the tool as 'app.tool'.")
    source = user_app(db, user, source_name)  # 404 unless it's yours
    if source.id == app.id:
        raise HTTPException(422, "An app already has its own tools; grants are for other apps' tools.")
    tool = child(db, Tool, source, tool_name)
    if db.scalar(select(Grant).where(Grant.tool_id == tool.id, Grant.to_app_id == app.id)):
        raise HTTPException(409, f"{body.tool} is already granted to {app.name!r}.")
    grant = Grant(tool_id=tool.id, to_app_id=app.id)
    db.add(grant)
    commit_change(db, user, [app], action="create", entity_type="grant", entity_name=f"{body.tool} -> {app.name}",
                  audit_app=app, after={"tool": body.tool, "to_app": app.name})
    return _grant_view(grant, tool, source)


@router.delete("/apps/{app_name}/grants/{grant_id}")
def delete_grant(app_name: str, grant_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    app = user_app(db, user, app_name)
    row = db.execute(select(Grant, Tool, App).join(Tool, Tool.id == Grant.tool_id).join(App, App.id == Tool.app_id)
                     .where(Grant.id == grant_id, Grant.to_app_id == app.id)).first()
    if row is None:
        raise NotFound(f"Grant {grant_id} not found in app {app.name!r}.")
    grant, tool, source = row
    name = f"{source.name}.{tool.name}"
    db.delete(grant)
    commit_change(db, user, [app], action="delete", entity_type="grant", entity_name=f"{name} -> {app.name}",
                  audit_app=app, before={"tool": name, "to_app": app.name})
    return {"deleted": grant_id}


# ── audit log ──────────────────────────────────────────────────────────────

@router.get("/audit")
def audit_log(app: str | None = Query(default=None), limit: int = Query(default=100, ge=1, le=500),
              db: Session = Depends(get_db), user: User = Depends(current_user)):
    q = select(AuditLog, App.name).outerjoin(App, App.id == AuditLog.app_id).where(AuditLog.user_id == user.id)
    if app is not None:
        q = q.where(AuditLog.app_id == user_app(db, user, app).id)
    rows = db.execute(q.order_by(AuditLog.id.desc()).limit(limit)).all()
    return [{"id": a.id, "app": app_name, "action": a.action, "entity_type": a.entity_type,
             "entity_name": a.entity_name, "before": a.before, "after": a.after, "created_at": a.created_at}
            for a, app_name in rows]
