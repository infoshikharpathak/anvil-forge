"""Item 2: accounts, DB sessions, CSRF, logout, expiry, login rate limit."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from bastion_server.accounts import AccountError, create_user
from bastion_server.models import LoginAttempt, User, UserSession

from .conftest import CSRF, PASSWORD, add_user, login


def test_passwords_are_argon2_and_validated(make_client):
    _, app = make_client()
    add_user(app)
    with app.state.sessionmaker() as db:
        stored = db.scalar(select(User)).password_hash
    assert stored.startswith("$argon2id$") and PASSWORD not in stored
    with app.state.sessionmaker.begin() as db:
        for email, pw, status in [("not-an-email", PASSWORD, 422), ("b@x.io", "short", 422), ("ANA@example.com", PASSWORD, 409)]:
            with pytest.raises(AccountError) as e:
                create_user(db, email, pw)
            assert e.value.status == status


def test_login_sets_a_strict_httponly_cookie_and_stores_only_a_hash(make_client):
    client, app = make_client()
    add_user(app)
    assert client.get("/v1/auth/me").status_code == 401
    resp = login(client, email="  Ana@Example.com ")  # emails are case/space-insensitive
    cookie = resp.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/" in cookie
    token = client.cookies.get("bastion_session")
    with app.state.sessionmaker() as db:
        stored = db.scalar(select(UserSession)).token_hash
    assert stored != token and len(stored) == 64
    assert client.get("/v1/auth/me").json()["email"] == "ana@example.com"


def test_writes_need_the_csrf_header(make_client):
    client, app = make_client()
    add_user(app)
    no_header = client.post("/v1/auth/login", json={"email": "ana@example.com", "password": PASSWORD})
    assert no_header.status_code == 403 and "X-Bastion" in no_header.json()["detail"]
    login(client)
    assert client.post("/v1/auth/logout").status_code == 403
    assert client.get("/v1/auth/me").status_code == 200  # reads don't need it


def test_wrong_password_and_unknown_email_look_the_same(make_client):
    client, app = make_client()
    add_user(app)
    bad = client.post("/v1/auth/login", json={"email": "ana@example.com", "password": "wrong password!"}, headers=CSRF)
    ghost = client.post("/v1/auth/login", json={"email": "ghost@example.com", "password": PASSWORD}, headers=CSRF)
    assert bad.status_code == ghost.status_code == 401
    assert bad.json() == ghost.json()


def test_logout_ends_the_session_server_side(make_client):
    client, app = make_client()
    add_user(app)
    login(client)
    token = client.cookies.get("bastion_session")
    assert client.post("/v1/auth/logout", headers=CSRF).status_code == 200
    assert client.get("/v1/auth/me").status_code == 401
    other, _ = make_client()  # a stolen copy of the old cookie is dead too
    other.cookies.set("bastion_session", token)
    assert other.get("/v1/auth/me").status_code == 401
    with app.state.sessionmaker() as db:
        assert db.scalars(select(UserSession)).all() == []


def test_expired_sessions_are_rejected_and_deleted(make_client):
    client, app = make_client()
    add_user(app)
    login(client)
    with app.state.sessionmaker.begin() as db:
        db.scalar(select(UserSession)).expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert client.get("/v1/auth/me").status_code == 401
    with app.state.sessionmaker() as db:
        assert db.scalars(select(UserSession)).all() == []


def test_sessions_survive_a_restart(make_client, db_url):
    client, app = make_client()
    add_user(app)
    login(client)
    from bastion_server.app import create_app
    from bastion_server.settings import Settings
    from dataclasses import replace
    from fastapi.testclient import TestClient

    restarted = TestClient(create_app(replace(Settings(), database_url=db_url)))
    restarted.cookies.set("bastion_session", client.cookies.get("bastion_session"))
    assert restarted.get("/v1/auth/me").status_code == 200


def test_login_rate_limit_per_email(make_client):
    client, app = make_client(login_max_per_email=3)
    add_user(app)
    for _ in range(3):
        r = client.post("/v1/auth/login", json={"email": "ana@example.com", "password": "nope nope nope"}, headers=CSRF)
        assert r.status_code == 401
    blocked = client.post("/v1/auth/login", json={"email": "ana@example.com", "password": PASSWORD}, headers=CSRF)
    assert blocked.status_code == 429  # even the right password, until the window passes
    with app.state.sessionmaker() as db:
        assert len(db.scalars(select(LoginAttempt)).all()) == 3  # the blocked try isn't counted


def test_login_rate_limit_per_ip_across_emails(make_client):
    client, app = make_client(login_max_per_ip=2)
    add_user(app)
    for email in ("x@example.com", "y@example.com"):
        client.post("/v1/auth/login", json={"email": email, "password": "nope nope nope"}, headers=CSRF)
    assert client.post("/v1/auth/login", json={"email": "ana@example.com", "password": PASSWORD}, headers=CSRF).status_code == 429


def test_failures_outside_the_window_dont_count(make_client):
    client, app = make_client(login_max_per_email=2)
    add_user(app)
    old = datetime.now(timezone.utc) - timedelta(hours=1)
    with app.state.sessionmaker.begin() as db:
        db.add_all([LoginAttempt(email="ana@example.com", ip="1.2.3.4", success=False, created_at=old) for _ in range(5)])
    login(client)


def test_signup_closed_by_default(make_client):
    client, _ = make_client()
    r = client.post("/v1/auth/signup", json={"email": "new@example.com", "password": PASSWORD}, headers=CSRF)
    assert r.status_code == 403


def test_signup_when_open_creates_and_signs_in(make_client):
    client, _ = make_client(signup_open=True)
    r = client.post("/v1/auth/signup", json={"email": "new@example.com", "password": PASSWORD}, headers=CSRF)
    assert r.status_code == 201 and client.get("/v1/auth/me").json()["email"] == "new@example.com"
    dup = client.post("/v1/auth/signup", json={"email": "new@example.com", "password": PASSWORD}, headers=CSRF)
    assert dup.status_code == 409
