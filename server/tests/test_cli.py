"""Item 9: the CLI that creates the first user."""

import io

from sqlalchemy import select

from garrison_hq.cli import _safe_url, main
from garrison_hq.db import make_engine, make_sessionmaker
from garrison_hq.models import User


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
                    headers={"X-Garrison": "1"})
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


def test_cleanup_traces_deletes_old_and_keeps_recent(db_url, monkeypatch, capsys, ana):
    from datetime import datetime, timedelta, timezone

    import pytest

    from garrison_hq.models import Trace

    from .conftest import make_ironclad, post_traces, trace_record

    client, app = ana
    make_ironclad(client)
    raw = client.post("/v1/apps/ironclad/keys", json={}).json()["key"]
    now = datetime.now(timezone.utc)
    old = [trace_record(started=now - timedelta(days=d)) for d in (31, 45, 400)]
    recent = [trace_record(started=now - timedelta(days=d)) for d in (0, 29)]
    assert post_traces(app, raw, old + recent).json()["accepted"] == 5
    monkeypatch.setenv("DATABASE_URL", db_url)

    assert main(["cleanup-traces", "--older-than", "30d", "--dry-run"]) == 0
    assert "Would delete 3 traces" in capsys.readouterr().out
    with app.state.sessionmaker() as db:
        assert db.query(Trace).count() == 5

    assert main(["cleanup-traces", "--older-than", "30d"]) == 0
    assert "Deleted 3 traces started before" in capsys.readouterr().out
    with app.state.sessionmaker() as db:
        assert {t.trace_id for t in db.query(Trace)} == {r["trace_id"] for r in recent}

    assert main(["cleanup-traces", "--older-than", "1"]) == 0  # plain number of days works too
    assert "Deleted 1 trace " in capsys.readouterr().out
    for bad in ("0d", "30x", "-3d", "thirty"):
        with pytest.raises(SystemExit):
            main(["cleanup-traces", "--older-than", bad])
