import os

import pytest
from sqlalchemy import text

os.environ.setdefault("OPENAI_API_KEY", "dummy-key-for-tests")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-key-for-tests")

# SQLite always; Postgres too when TEST_POSTGRES_URL is set (CI runs a service container).
_DB_PARAMS = ["sqlite"] + (["postgres"] if os.environ.get("TEST_POSTGRES_URL") else [])


def _reset_postgres(url: str) -> None:
    from anvil_forge_server.db import make_engine

    engine = make_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    engine.dispose()


@pytest.fixture(params=_DB_PARAMS)
def db_url(request, tmp_path):
    if request.param == "sqlite":
        return f"sqlite:///{tmp_path / 'server.db'}"
    url = os.environ["TEST_POSTGRES_URL"]
    _reset_postgres(url)
    return url


@pytest.fixture
def migrated(db_url):
    from anvil_forge_server.db import make_engine, make_sessionmaker, migrate

    migrate(db_url)
    engine = make_engine(db_url)
    yield engine, make_sessionmaker(engine)
    engine.dispose()


from dataclasses import replace  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

CSRF = {"X-Anvil-Forge": "1"}
PASSWORD = "correct horse battery"


@pytest.fixture
def make_client(db_url):
    """make_client(**settings) -> (TestClient, app). Each call is a separate browser."""
    from anvil_forge_server.app import create_app
    from anvil_forge_server.settings import Settings

    apps = {}

    def _make(**overrides):
        key = tuple(sorted(overrides.items()))
        if key not in apps:
            apps[key] = create_app(replace(Settings(), database_url=db_url, **overrides))
        return TestClient(apps[key]), apps[key]

    yield _make
    for app in apps.values():
        app.state.engine.dispose()


def add_user(app, email="ana@example.com", password=PASSWORD):
    from anvil_forge_server.accounts import create_user

    with app.state.sessionmaker.begin() as db:
        return create_user(db, email, password).id


def login(client, email="ana@example.com", password=PASSWORD):
    resp = client.post("/v1/auth/login", json={"email": email, "password": password}, headers=CSRF)
    assert resp.status_code == 200, resp.text
    return resp


@pytest.fixture
def ana(make_client):
    """A signed-in client for ana@example.com (client, app)."""
    client, app = make_client()
    add_user(app)
    login(client)
    client.headers.update(CSRF)
    return client, app


def as_other_user(make_client, email="bob@example.com"):
    client, app = make_client()
    add_user(app, email=email)
    login(client, email=email)
    client.headers.update(CSRF)
    return client


TOOL = {"name": "get_accounts", "path": "/accounts", "method": "GET", "trust": "trusted",
        "description": "List accounts.",
        "parameters": {"type": "object", "properties": {"user_id": {"type": "string"}}, "required": ["user_id"]}}
AGENT = {"name": "banking_agent", "instructions": "You are a banking assistant.", "backend": "openai",
         "model": "gpt-4o-mini", "tools": ["get_accounts"]}


def make_ironclad(client):
    assert client.post("/v1/apps", json={"name": "ironclad", "base_url": "http://bank.test"}).status_code == 201
    assert client.post("/v1/apps/ironclad/tools", json=TOOL).status_code == 201
    assert client.post("/v1/apps/ironclad/agents", json=AGENT).status_code == 201
