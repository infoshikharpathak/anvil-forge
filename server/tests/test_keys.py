"""Item 4: app keys: shown once, stored as SHA-256, several per app, revocable."""

import hashlib

from sqlalchemy import select

from garrison_hq.keys import app_for_key
from garrison_hq.models import AppKey

from .conftest import as_other_user, make_ironclad


def test_key_is_shown_once_and_stored_hashed(ana):
    client, app = ana
    make_ironclad(client)
    created = client.post("/v1/apps/ironclad/keys", json={"name": "agent-service"})
    assert created.status_code == 201
    raw = created.json()["key"]
    assert raw.startswith("gk_") and len(raw) == 46  # "gk_" + 43 url-safe chars
    listed = client.get("/v1/apps/ironclad/keys").json()
    assert "key" not in listed[0] and listed[0]["prefix"] == raw[:12] and listed[0]["active"]
    assert raw not in str(listed)
    with app.state.sessionmaker() as db:
        row = db.scalar(select(AppKey))
    assert row.key_hash == hashlib.sha256(raw.encode()).hexdigest() and raw not in str(vars(row))
    audit = client.get("/v1/audit").json()[0]
    assert audit["entity_type"] == "app_key" and raw not in str(audit)


def test_several_keys_for_rotation_and_revocation(ana):
    client, app = ana
    make_ironclad(client)
    old = client.post("/v1/apps/ironclad/keys", json={"name": "old"}).json()
    new = client.post("/v1/apps/ironclad/keys", json={"name": "new"}).json()
    with app.state.sessionmaker() as db:
        assert app_for_key(db, old["key"]) and app_for_key(db, new["key"])
    revoked = client.delete(f"/v1/apps/ironclad/keys/{old['id']}").json()
    assert revoked["revoked_at"] and not revoked["active"]
    with app.state.sessionmaker() as db:
        assert app_for_key(db, old["key"]) is None
        assert app_for_key(db, new["key"])[0].name == "ironclad"
    assert client.delete("/v1/apps/ironclad/keys/999").status_code == 404


def test_last_used_at_recorded(ana):
    client, app = ana
    make_ironclad(client)
    raw = client.post("/v1/apps/ironclad/keys", json={}).json()["key"]
    with app.state.sessionmaker.begin() as db:
        app_for_key(db, raw)
    assert client.get("/v1/apps/ironclad/keys").json()[0]["last_used_at"] is not None


def test_malformed_and_unknown_keys_rejected(ana):
    _, app = ana
    with app.state.sessionmaker() as db:
        for raw in (None, "", "sk-live-123", "gk_doesnotexist"):
            assert app_for_key(db, raw) is None


def test_other_users_cant_see_or_make_keys(ana, make_client):
    client, _ = ana
    make_ironclad(client)
    client.post("/v1/apps/ironclad/keys", json={})
    bob = as_other_user(make_client)
    assert bob.get("/v1/apps/ironclad/keys").status_code == 404
    assert bob.post("/v1/apps/ironclad/keys", json={}).status_code == 404
    assert bob.delete("/v1/apps/ironclad/keys/1").status_code == 404
