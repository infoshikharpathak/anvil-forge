"""Item 1: schema via Alembic on SQLite (and Postgres when available)."""

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from anvil_forge_server.db import alembic_config, make_engine, migrate
from anvil_forge_server.models import (
    Agent, App, AuditLog, Base, BundleRevision, Grant, PromptVersion, Tool, User,
)

TABLES = {"users", "user_sessions", "login_attempts", "apps", "app_keys", "tools", "agents",
          "prompt_versions", "flows", "grants", "bundle_revisions", "audit_log"}


def test_migration_creates_every_table(migrated):
    engine, _ = migrated
    assert TABLES <= set(inspect(engine).get_table_names())


def test_models_and_migrations_agree(migrated):
    engine, _ = migrated
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == [], diff


def test_downgrade_and_upgrade_round_trip(db_url):
    migrate(db_url)
    command.downgrade(alembic_config(db_url), "base")
    engine = make_engine(db_url)
    assert not (TABLES & set(inspect(engine).get_table_names()))
    migrate(db_url)
    assert TABLES <= set(inspect(engine).get_table_names())
    engine.dispose()


def test_agent_prompt_pointer_and_cascades(migrated):
    _, Session = migrated
    with Session.begin() as s:
        user = User(email="a@example.com", password_hash="x")
        s.add(user)
        s.flush()
        app = App(owner_id=user.id, name="ironclad", settings={}, classifier={})
        s.add(app)
        s.flush()
        tool = Tool(app_id=app.id, name="get_accounts", spec={"path": "/accounts"})
        agent = Agent(app_id=app.id, name="banking_agent", spec={"tools": ["get_accounts"]})
        s.add_all([tool, agent])
        s.flush()
        v1 = PromptVersion(agent_id=agent.id, version=1, label="v1", instructions="Be helpful.")
        s.add(v1)
        s.flush()
        agent.active_version_id = v1.id
        other = App(owner_id=user.id, name="notify", settings={}, classifier={})
        s.add(other)
        s.flush()
        s.add_all([
            Grant(tool_id=tool.id, to_app_id=other.id),
            BundleRevision(app_id=app.id, revision=1, etag="e" * 64, bundle={"apps": {}}),
            AuditLog(user_id=user.id, app_id=app.id, action="create", entity_type="app", entity_name="ironclad"),
        ])
        app_id = app.id
    with Session.begin() as s:
        assert s.get(Agent, agent.id).active_version_id == v1.id
        s.delete(s.get(App, app_id))
    with Session() as s:
        for model in (Tool, Agent, PromptVersion, Grant, BundleRevision):
            assert s.scalars(select(model)).all() == [], model.__name__
        assert s.scalars(select(AuditLog)).one().app_id is None  # audit outlives the app


def test_app_names_unique_per_owner(migrated):
    _, Session = migrated
    with Session.begin() as s:
        a, b = User(email="a@x.io", password_hash="x"), User(email="b@x.io", password_hash="x")
        s.add_all([a, b])
        s.flush()
        s.add_all([App(owner_id=a.id, name="ironclad"), App(owner_id=b.id, name="ironclad")])  # fine
    with pytest.raises(IntegrityError):
        with Session.begin() as s:
            s.add(App(owner_id=a.id, name="ironclad"))


def test_foreign_keys_enforced(migrated):
    _, Session = migrated
    with pytest.raises(IntegrityError):
        with Session.begin() as s:
            s.add(Tool(app_id=999, name="orphan", spec={}))
