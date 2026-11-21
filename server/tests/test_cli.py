"""Item 9: the CLI that creates the first user."""

import io

from sqlalchemy import select

from anvil_forge_server.cli import _safe_url, main
from anvil_forge_server.db import make_engine, make_sessionmaker
from anvil_forge_server.models import User


def test_create_user_from_stdin_then_log_in(db_url, monkeypatch, capsys, make_client):
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setattr("sys.stdin", io.StringIO("correct horse battery\n"))
    assert main(["create-user", "--email", "Admin@Example.com", "--password-stdin"]) == 0
    assert "Created admin@example.com" in capsys.readouterr().out
    engine = make_engine(db_url)
    with make_sessionmaker(engine)() as db:
        user = db.scalar(select(User))
    engine.dispose()
    assert user.password_hash.startswith("$argon2id$")
    client, _ = make_client()
    r = client.post("/v1/auth/login", json={"email": "admin@example.com", "password": "correct horse battery"},
                    headers={"X-Anvil-Forge": "1"})
    assert r.status_code == 200


def test_create_user_rejects_bad_input(db_url, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
    assert main(["create-user", "--email", "a@example.com", "--password-stdin"]) == 1
    assert "at least 10 characters" in capsys.readouterr().err


def test_migrate_command(db_url, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", db_url)
    assert main(["migrate"]) == 0
    assert "up to date" in capsys.readouterr().out


def test_database_passwords_are_not_printed():
    assert _safe_url("postgresql+psycopg://forge:s3cret@db:5432/af") == "postgresql+psycopg://forge:***@db:5432/af"
    assert _safe_url("sqlite:///./x.db") == "sqlite:///./x.db"
