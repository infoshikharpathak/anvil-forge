"""Trace ingestion (POST /v1/traces): app-key auth, write-only.

The SDK's DashboardSink sends batches of trace records. Each record is
checked on its own, so one bad record doesn't cost the rest of the batch:

- ``app`` must be the key's app;
- the agent (for a flow run, the flow and its entry agent) must be
  registered in that app;
- ``trace_id`` must be a UUID.

Rejected records come back in the response with the reason. A record whose
``run_id`` is already stored is skipped, so a batch the SDK retries after a
lost response isn't stored twice.

Rate limit: ``GARRISON_TRACE_RATE`` traces per minute per app key (default
100), counted from what's stored, so it holds across processes. A batch that
would go over is refused whole with 429 and a Retry-After saying when it
fits.
"""

from __future__ import annotations

import json
import math
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..deps import get_db, get_settings
from ..keys import app_for_key
from ..models import Agent, App, AppKey, Flow, Trace
from ..security import aware
from ..settings import Settings

router = APIRouter(prefix="/v1", tags=["traces"])

MAX_CONTENT_BYTES = 256 * 1024  # per record; larger content is a client bug, not a trace
MAX_LIST = 2000


class TraceIn(BaseModel):
    """One run, as DashboardSink sends it. Unknown fields are ignored so an
    SDK newer than the server can still report."""

    model_config = ConfigDict(extra="ignore")

    trace_id: str
    run_id: str = Field(min_length=1, max_length=64)
    app: str = Field(min_length=1, max_length=100)
    kind: Literal["agent", "flow"] = "agent"
    agent: str = Field(min_length=1, max_length=200)
    flow: str | None = Field(default=None, max_length=200)
    session_id: str | None = Field(default=None, max_length=200)
    bundle_revision: int | None = None
    caller_app: str | None = Field(default=None, max_length=100)
    parent_trace_id: str | None = Field(default=None, max_length=64)
    chain: list[str] = Field(default_factory=list, max_length=20)
    child_traces: list[dict[str, Any]] = Field(default_factory=list, max_length=200)
    prompt_version: str | None = Field(default=None, max_length=100)
    started_at: datetime
    ended_at: datetime
    duration_ms: float | None = Field(default=None, ge=0)
    status: Literal["success", "blocked", "error"]
    model: str | None = Field(default=None, max_length=100)
    token_input: int = Field(default=0, ge=0)
    token_output: int = Field(default=0, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)  # None: no price for the model(s)
    unpriced_models: list[str] = Field(default_factory=list, max_length=200)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list, max_length=MAX_LIST)
    steps: list[dict[str, Any]] = Field(default_factory=list, max_length=MAX_LIST)
    agents: list[dict[str, Any]] = Field(default_factory=list, max_length=200)
    violations: list[dict[str, Any]] = Field(default_factory=list, max_length=MAX_LIST)
    scans: list[dict[str, Any]] = Field(default_factory=list, max_length=MAX_LIST)
    hook_denials: int = Field(default=0, ge=0)
    content: dict[str, Any] | None = None

    @field_validator("trace_id")
    @classmethod
    def _uuid(cls, v: str) -> str:
        try:
            return str(uuid.UUID(v))
        except (ValueError, AttributeError, TypeError):
            raise ValueError("trace_id must be a UUID") from None

    @field_validator("started_at", "ended_at")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return v.replace(tzinfo=timezone.utc) if v.tzinfo is None else v.astimezone(timezone.utc)


def key_and_app(authorization: str | None = Header(default=None), db: Session = Depends(get_db)) -> tuple[App, AppKey]:
    raw = authorization[7:].strip() if authorization and authorization.lower().startswith("bearer ") else None
    found = app_for_key(db, raw)
    if found is None:
        raise HTTPException(401, "A valid app key is required (Authorization: Bearer gk_...).",
                            headers={"WWW-Authenticate": "Bearer"})
    return found


def _local_name(name: str, app: str) -> str | None:
    """'ironclad.banking_agent' or 'banking_agent' -> 'banking_agent';
    another app's name -> None."""
    if "." in name:
        owner, _, rest = name.partition(".")
        return rest if owner == app else None
    return name


def _check(rec: TraceIn, app: App, agents: dict[str, int], flows: set[str]) -> tuple[int | None, str, str | None]:
    """(agent_id, agent_name, flow_name), or raises ValueError with the reason."""
    if rec.app != app.name:
        raise ValueError(f"app {rec.app!r} doesn't match the key's app {app.name!r}")
    flow_name = None
    if rec.kind == "flow":
        flow_name = _local_name(rec.flow or "", app.name)
        if not flow_name or flow_name not in flows:
            raise ValueError(f"flow {rec.flow!r} is not registered in {app.name!r}")
    agent_name = _local_name(rec.agent, app.name)
    if agent_name is None or agent_name not in agents:
        raise ValueError(f"agent {rec.agent!r} is not registered in {app.name!r}")
    if rec.content is not None and len(json.dumps(rec.content, default=str)) > MAX_CONTENT_BYTES:
        raise ValueError(f"content is larger than {MAX_CONTENT_BYTES} bytes")
    if rec.ended_at < rec.started_at:
        raise ValueError("ended_at is before started_at")
    return agents[agent_name], agent_name, flow_name


def _retry_after(db: Session, key: AppKey, now: datetime, used: int, incoming: int, limit: int) -> int:
    """Seconds until *incoming* more traces fit in the one-minute window."""
    window_start = now - timedelta(minutes=1)
    must_expire = used + incoming - limit  # this many of the oldest must leave the window
    times = db.scalars(
        select(Trace.received_at).where(Trace.app_key_id == key.id, Trace.received_at > window_start)
        .order_by(Trace.received_at).limit(must_expire)
    ).all()
    if not times:
        return 1
    leaves_at = aware(times[-1]) + timedelta(minutes=1)
    return max(1, math.ceil((leaves_at - now).total_seconds()))


@router.post("/traces", status_code=202)
def ingest(
    request: Request,
    body: Any = Body(...),
    found: tuple[App, AppKey] = Depends(key_and_app),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    app, key = found
    if not isinstance(body, list):
        raise HTTPException(422, "Send a JSON list of trace records.")
    if len(body) > settings.trace_batch_max:
        raise HTTPException(422, f"At most {settings.trace_batch_max} traces per request (got {len(body)}).")

    agents = dict(db.execute(select(Agent.name, Agent.id).where(Agent.app_id == app.id)).all())
    flows = set(db.scalars(select(Flow.name).where(Flow.app_id == app.id)))

    rejected: list[dict[str, Any]] = []
    valid: list[tuple[TraceIn, int | None, str, str | None]] = []
    for i, raw in enumerate(body):
        trace_id = raw.get("trace_id") if isinstance(raw, dict) else None
        try:
            rec = TraceIn.model_validate(raw)
            valid.append((rec, *_check(rec, app, agents, flows)))
        except ValidationError as e:
            errors = "; ".join(f"{'.'.join(map(str, err['loc'])) or 'record'}: {err['msg']}" for err in e.errors()[:5])
            rejected.append({"index": i, "trace_id": trace_id, "error": errors})
        except ValueError as e:
            rejected.append({"index": i, "trace_id": trace_id, "error": str(e)})

    # Skip runs already stored (a retried batch), and repeats within this batch.
    run_ids = [rec.run_id for rec, *_ in valid]
    stored = set(db.scalars(select(Trace.run_id).where(Trace.app_id == app.id, Trace.run_id.in_(run_ids)))) if run_ids else set()
    fresh, duplicates = [], 0
    for item in valid:
        if item[0].run_id in stored:
            duplicates += 1
        else:
            stored.add(item[0].run_id)
            fresh.append(item)

    now = datetime.now(timezone.utc)
    limit = settings.trace_rate_per_minute
    used = db.scalar(select(func.count()).select_from(Trace).where(
        Trace.app_key_id == key.id, Trace.received_at > now - timedelta(minutes=1))) or 0
    if fresh and used + len(fresh) > limit:
        retry = _retry_after(db, key, now, used, len(fresh), limit) if len(fresh) <= limit else 60
        return JSONResponse(
            {"detail": f"Rate limit: {limit} traces per minute per app key ({used} in the last minute)."},
            status_code=429, headers={"Retry-After": str(retry)},
        )

    for rec, agent_id, agent_name, flow_name in fresh:
        duration = rec.duration_ms
        if duration is None:
            duration = (rec.ended_at - rec.started_at).total_seconds() * 1000
        db.add(Trace(
            app_id=app.id, agent_id=agent_id, agent_name=agent_name, flow_name=flow_name, app_key_id=key.id,
            trace_id=rec.trace_id, run_id=rec.run_id, session_id=rec.session_id,
            bundle_revision=rec.bundle_revision, prompt_version=rec.prompt_version,
            caller_app=rec.caller_app, parent_trace_id=rec.parent_trace_id, chain=list(rec.chain),
            started_at=rec.started_at, ended_at=rec.ended_at, duration_ms=round(duration, 1),
            status=rec.status, model=rec.model, token_input=rec.token_input, token_output=rec.token_output,
            cost_usd=rec.cost_usd, unpriced=rec.cost_usd is None or bool(rec.unpriced_models),
            tool_calls=rec.tool_calls, steps=rec.steps,
            summary={"kind": rec.kind, "agents": rec.agents, "violations": rec.violations,
                     "scans": rec.scans, "hook_denials": rec.hook_denials,
                     "unpriced_models": rec.unpriced_models, "child_traces": rec.child_traces},
            content=rec.content, received_at=now,
        ))
    db.flush()
    return {"accepted": len(fresh), "duplicates": duplicates, "rejected": rejected}
