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
