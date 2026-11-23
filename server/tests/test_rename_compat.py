"""Setups from before the rename keep working: bk_ app keys, the old CSRF
header and the old env var names (with a one-time deprecation warning)."""

import hashlib

import pytest

from garrison import compat
from garrison_hq.keys import app_for_key
from garrison_hq.models import AppKey
from garrison_hq.settings import Settings

from .conftest import make_ironclad


def test_new_keys_are_gk_and_old_bk_keys_still_work(ana):
    client, app = ana
    make_ironclad(client)
    raw = client.post("/v1/apps/ironclad/keys", json={}).json()["key"]
    assert raw.startswith("gk_")
    old = "bk_" + "o" * 43  # a key issued before the rename
    with app.state.sessionmaker.begin() as db:
        row = db.query(AppKey).one()
        db.add(AppKey(app_id=row.app_id, name="pre-rename", key_hash=hashlib.sha256(old.encode()).hexdigest(),
                      prefix=old[:12], created_by=row.created_by))
    with app.state.sessionmaker() as db:
        assert app_for_key(db, old)[0].name == "ironclad"
        assert app_for_key(db, "xk_" + "o" * 43) is None


def test_old_csrf_header_is_accepted(make_client):
    from .conftest import PASSWORD, add_user

    client, app = make_client()
    add_user(app)
    body = {"email": "ana@example.com", "password": PASSWORD}
    assert client.post("/v1/auth/login", json=body).status_code == 403
    assert client.post("/v1/auth/login", json=body, headers={"X-Bastion": "1"}).status_code == 200


def test_old_env_names_fall_back_with_one_warning(monkeypatch, caplog):
    monkeypatch.delenv("GARRISON_TRACE_RATE", raising=False)
    monkeypatch.setenv("BASTION_TRACE_RATE", "7")
    monkeypatch.setattr(compat, "_warned", set())
    with pytest.warns(DeprecationWarning, match="BASTION_TRACE_RATE is deprecated; set GARRISON_TRACE_RATE"):
        assert Settings().trace_rate_per_minute == 7
    caplog.clear()
    assert Settings().trace_rate_per_minute == 7  # no second warning
    assert "deprecated" not in caplog.text
    monkeypatch.setenv("GARRISON_TRACE_RATE", "9")  # the new name wins
    assert Settings().trace_rate_per_minute == 9
