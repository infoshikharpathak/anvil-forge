"""Database tables (SQLAlchemy 2 ORM). Schema changes go through Alembic."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


Timestamp = DateTime(timezone=True)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(Timestamp, default=utcnow)


class UserSession(Base):
    """A signed-in browser. The cookie holds a random token; only its SHA-256 is stored."""

    __tablename__ = "user_sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(Timestamp, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(Timestamp)
    last_seen_at: Mapped[datetime] = mapped_column(Timestamp, default=utcnow)
    user: Mapped[User] = relationship()


class LoginAttempt(Base):
    __tablename__ = "login_attempts"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320))
    ip: Mapped[str] = mapped_column(String(64))
    success: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(Timestamp, default=utcnow)
    __table_args__ = (
        Index("ix_login_attempts_email_time", "email", "created_at"),
        Index("ix_login_attempts_ip_time", "ip", "created_at"),
    )


class App(Base):
    __tablename__ = "apps"
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    base_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    # {header, secret_env, prefix}: env var NAMES only, never secret values.
    auth: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # {granting app: "env:VAR"}: credentials OTHER apps issued to this one, for
    # the tools they granted it. Env var NAMES only, never secret values.
    credentials: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, server_default="{}")
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    classifier: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # SDK dashboard options carried in the bundle ({include_content}). Never a URL or key.
    dashboard: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(Timestamp, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(Timestamp, default=utcnow, onupdate=utcnow)
    __table_args__ = (UniqueConstraint("owner_id", "name", name="uq_apps_owner_name"),)


class AppKey(Base):
    __tablename__ = "app_keys"
    id: Mapped[int] = mapped_column(primary_key=True)
    app_id: Mapped[int] = mapped_column(ForeignKey("apps.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100), default="")
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    prefix: Mapped[str] = mapped_column(String(16))  # shown in lists to tell keys apart
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(Timestamp, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(Timestamp, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(Timestamp, nullable=True)


class Tool(Base):
    __tablename__ = "tools"
    id: Mapped[int] = mapped_column(primary_key=True)
    app_id: Mapped[int] = mapped_column(ForeignKey("apps.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    # Anvil tool fields (path/endpoint, method, trust, parameters, redact_fields, ...).
    spec: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(Timestamp, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(Timestamp, default=utcnow, onupdate=utcnow)
    __table_args__ = (UniqueConstraint("app_id", "name", name="uq_tools_app_name"),)


class Agent(Base):
    __tablename__ = "agents"
    id: Mapped[int] = mapped_column(primary_key=True)
    app_id: Mapped[int] = mapped_column(ForeignKey("apps.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    # Everything but the prompt: backend, model, role, description, tools,
    # sequences, strictness, trust_overrides. The prompt lives in prompt_versions.
    spec: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    active_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("prompt_versions.id", use_alter=True, name="fk_agents_active_version", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(Timestamp, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(Timestamp, default=utcnow, onupdate=utcnow)
    __table_args__ = (UniqueConstraint("app_id", "name", name="uq_agents_app_name"),)


class PromptVersion(Base):
    """Immutable. Editing instructions adds a row; rollback moves the agent's pointer."""

    __tablename__ = "prompt_versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    label: Mapped[str] = mapped_column(String(100))
    instructions: Mapped[str] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(Timestamp, default=utcnow)
    __table_args__ = (
        UniqueConstraint("agent_id", "version", name="uq_prompt_versions_agent_version"),
        UniqueConstraint("agent_id", "label", name="uq_prompt_versions_agent_label"),
    )


class Flow(Base):
    __tablename__ = "flows"
    id: Mapped[int] = mapped_column(primary_key=True)
    app_id: Mapped[int] = mapped_column(ForeignKey("apps.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    spec: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # entry, nodes, edges
    created_at: Mapped[datetime] = mapped_column(Timestamp, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(Timestamp, default=utcnow, onupdate=utcnow)
    __table_args__ = (UniqueConstraint("app_id", "name", name="uq_flows_app_name"),)


class Grant(Base):
    """Lets *to_app* use one of another app's tools (both owned by the same user)."""

    __tablename__ = "grants"
    id: Mapped[int] = mapped_column(primary_key=True)
    tool_id: Mapped[int] = mapped_column(ForeignKey("tools.id", ondelete="CASCADE"), index=True)
    to_app_id: Mapped[int] = mapped_column(ForeignKey("apps.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(Timestamp, default=utcnow)
    __table_args__ = (UniqueConstraint("tool_id", "to_app_id", name="uq_grants_tool_to_app"),)


class BundleRevision(Base):
    __tablename__ = "bundle_revisions"
    id: Mapped[int] = mapped_column(primary_key=True)
    app_id: Mapped[int] = mapped_column(ForeignKey("apps.id", ondelete="CASCADE"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    etag: Mapped[str] = mapped_column(String(64))
    bundle: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(Timestamp, default=utcnow)
    __table_args__ = (UniqueConstraint("app_id", "revision", name="uq_bundle_revisions_app_revision"),)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    app_id: Mapped[int | None] = mapped_column(ForeignKey("apps.id", ondelete="SET NULL"), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(50))
    entity_type: Mapped[str] = mapped_column(String(50))
    entity_name: Mapped[str] = mapped_column(String(200))
    before: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(Timestamp, default=utcnow, index=True)


class Trace(Base):
    """One agent or flow run, as the SDK's DashboardSink sends it: metadata
    always, content (prompts, inputs, outputs, tool params) only when the
    app opted in. The app's local trace file stays the full record."""

    __tablename__ = "traces"
    id: Mapped[int] = mapped_column(primary_key=True)
    app_id: Mapped[int] = mapped_column(ForeignKey("apps.id", ondelete="CASCADE"))
    # Registered at ingest time. SET NULL keeps the history if the agent is deleted later.
    agent_id: Mapped[int | None] = mapped_column(ForeignKey("agents.id", ondelete="SET NULL"), nullable=True)
    agent_name: Mapped[str] = mapped_column(String(100))
    flow_name: Mapped[str | None] = mapped_column(String(100), nullable=True)  # set for flow runs
    app_key_id: Mapped[int | None] = mapped_column(ForeignKey("app_keys.id", ondelete="SET NULL"), nullable=True)
    trace_id: Mapped[str] = mapped_column(String(36))
    run_id: Mapped[str] = mapped_column(String(64))
    session_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    bundle_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Agent-to-agent: the app that called this run (from its authenticated
    # key), the caller's trace, and the call chain (informational past the
    # direct caller). Child traces (apps this run called) are in summary.
    caller_app: Mapped[str | None] = mapped_column(String(100), nullable=True)
    parent_trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    chain: Mapped[list[Any]] = mapped_column(JSON, default=list, server_default="[]")
    prompt_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    started_at: Mapped[datetime] = mapped_column(Timestamp)
    ended_at: Mapped[datetime] = mapped_column(Timestamp)
    duration_ms: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(16))  # success | blocked | error
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    token_input: Mapped[int] = mapped_column(Integer, default=0)
    token_output: Mapped[int] = mapped_column(Integer, default=0)
    # None: no model in the trace had a price. unpriced: some part had none
    # (it's left out of cost_usd), so the cost shown is partial or unknown.
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)
    unpriced: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    tool_calls: Mapped[list[Any]] = mapped_column(JSON, default=list)
    steps: Mapped[list[Any]] = mapped_column(JSON, default=list)
    # Per-agent rows, violations, scans and hook denial count (metadata only).
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    content: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    received_at: Mapped[datetime] = mapped_column(Timestamp, default=utcnow)
    __table_args__ = (
        # A retried batch must not store a run twice.
        UniqueConstraint("app_id", "run_id", name="uq_traces_app_run"),
        Index("ix_traces_app_started", "app_id", "started_at"),
        Index("ix_traces_app_trace", "app_id", "trace_id"),
        Index("ix_traces_app_session", "app_id", "session_id"),
        Index("ix_traces_app_agent_started", "app_id", "agent_name", "started_at"),
        Index("ix_traces_key_received", "app_key_id", "received_at"),
    )
