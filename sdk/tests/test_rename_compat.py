"""The SDK reads the old Bastion env var and header names as a fallback."""

import warnings

import pytest

from garrison import compat
from garrison.a2a import CallerKeys, hash_key, new_key


@pytest.fixture(autouse=True)
def fresh_warnings(monkeypatch):
    monkeypatch.setattr(compat, "_warned", set())


def test_getenv_prefers_new_name(monkeypatch):
    monkeypatch.setenv("GARRISON_URL", "http://new")
    monkeypatch.setenv("BASTION_URL", "http://old")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert compat.getenv("GARRISON_URL") == "http://new"


def test_getenv_falls_back_and_warns_once(monkeypatch):
    monkeypatch.delenv("GARRISON_KEY", raising=False)
    monkeypatch.setenv("BASTION_KEY", "gk_x")
    with pytest.warns(DeprecationWarning, match="BASTION_KEY is deprecated"):
        assert compat.getenv("GARRISON_KEY") == "gk_x"
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert compat.getenv("GARRISON_KEY") == "gk_x"
    monkeypatch.delenv("BASTION_KEY")
    assert compat.getenv("GARRISON_KEY", "default") == "default"


def test_old_dashboard_env_names_still_turn_on_the_dashboard(monkeypatch):
    from garrison import Platform

    from .conftest import base_config

    monkeypatch.setenv("IRONCLAD_TOKEN", "tok")
    monkeypatch.delenv("GARRISON_URL", raising=False)
    monkeypatch.delenv("GARRISON_KEY", raising=False)
    monkeypatch.setenv("BASTION_URL", "http://env-svc")
    monkeypatch.setenv("BASTION_KEY", "bk_" + "e" * 43)
    with pytest.warns(DeprecationWarning):
        p = Platform.from_config(base_config(), app="ironclad")
    assert p.dashboard.url == "http://env-svc"


def test_a2a_reads_old_header_names():
    key = new_key()
    keys = CallerKeys({hash_key(key): {"app": "hub"}})
    old = keys.authenticate({"Authorization": f"Bearer {key}", "X-Bastion-Call-Chain": "hub.assistant",
                             "X-Bastion-Trace-Id": "t-1"})
    new = keys.authenticate({"Authorization": f"Bearer {key}", "X-Garrison-Call-Chain": "hub.assistant",
                             "X-Garrison-Trace-Id": "t-2", "X-Bastion-Trace-Id": "t-1"})
    assert (old.chain, old.parent_trace_id) == (["hub.assistant"], "t-1")
    assert (new.chain, new.parent_trace_id) == (["hub.assistant"], "t-2")
