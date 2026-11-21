"""4c item 2: when Platform adds a DashboardSink, and what it sends from a real run."""

import json
import logging

import httpx
import pytest

from bastion import DashboardSink, LocalSink, Platform

from .conftest import BASE, base_config
from .llm import ScriptedOpenAI
from .test_remote import KEY, URL, FakeService, platform
from .upstream import Upstream

SUPPORT = BASE["agents"]["ironclad.support_agent"]["instructions"]


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.delenv("BASTION_URL", raising=False)
    monkeypatch.delenv("BASTION_KEY", raising=False)
    monkeypatch.setenv("IRONCLAD_TOKEN", "tok")


def sinks(p):
    return [type(s).__name__ for s in p._exporter.sinks]


def with_dashboard(**section):
    cfg = base_config()
    cfg["dashboard"] = section
    return cfg


def test_library_mode_without_dashboard_or_env_stays_local():
    p = Platform.from_config(base_config(), app="ironclad")
    assert sinks(p) == ["LocalSink"] and p.dashboard is None


def test_library_mode_dashboard_section():
    p = Platform.from_config(with_dashboard(url="http://svc", key="bk_" + "a" * 43, include_content=True,
                                            flush_interval=2, batch_size=10), app="ironclad")
    assert sinks(p) == ["LocalSink", "DashboardSink"]
    d = p.dashboard
    assert (d.url, d.include_content, d.flush_interval, d.batch_size) == ("http://svc", True, 2, 10)


def test_library_mode_section_takes_url_and_key_from_env(monkeypatch):
    monkeypatch.setenv("BASTION_URL", "http://env-svc/")
    monkeypatch.setenv("BASTION_KEY", "bk_" + "e" * 43)
    p = Platform.from_config(with_dashboard(include_content=False), app="ironclad")
    assert p.dashboard.url == "http://env-svc" and p.dashboard._key == "bk_" + "e" * 43
    assert p.dashboard.include_content is False


def test_library_mode_env_alone_turns_it_on_metadata_only(monkeypatch):
    monkeypatch.setenv("BASTION_URL", "http://env-svc")
    monkeypatch.setenv("BASTION_KEY", "bk_" + "e" * 43)
    p = Platform.from_config(base_config(), app="ironclad")
    assert p.dashboard is not None and p.dashboard.include_content is False
    assert Platform.from_config(base_config(), app="ironclad", dashboard=False).dashboard is None
    assert sinks(Platform.from_config(base_config(), app="ironclad", sinks=[LocalSink()])) == ["LocalSink"]


def test_library_mode_section_without_a_key_warns(caplog):
    with caplog.at_level(logging.WARNING):
        p = Platform.from_config(with_dashboard(url="http://svc"), app="ironclad")
    assert p.dashboard is None and "no key" in caplog.text


def test_dashboard_section_is_validated():
    from bastion import ConfigError

    with pytest.raises(ConfigError):
        Platform.from_config(with_dashboard(include_contents=True), app="ironclad")


def test_remote_mode_adds_a_dashboard_sink_to_the_same_service(tmp_path):
    p = platform(FakeService(base_config()), tmp_path)
    assert sinks(p) == ["LocalSink", "DashboardSink"]
    assert p.dashboard.url == URL and p.dashboard._key == KEY and p.dashboard.include_content is False
    assert platform(FakeService(base_config()), tmp_path, dashboard=False).dashboard is None


async def test_remote_mode_follows_the_bundles_content_setting(tmp_path):
    service = FakeService(with_dashboard(include_content=True))
    p = platform(service, tmp_path)
    assert p.dashboard.include_content is True
    service.publish(with_dashboard(include_content=False), 2)
    assert await p.refresh() is True
    assert p.dashboard.include_content is False
    # an explicit choice in code wins over the bundle
    forced = platform(FakeService(with_dashboard(include_content=True)), tmp_path, include_content=False)
    assert forced.dashboard.include_content is False
    await p.close()


async def test_a_run_reaches_the_service_and_close_flushes(tmp_path):
    posted = []

    def traces(request: httpx.Request):
        posted.extend(json.loads(request.content))
        return httpx.Response(202, json={"accepted": 1, "duplicates": 0, "rejected": []})

    llm = ScriptedOpenAI({SUPPORT: [{"tool": "get_accounts", "args": {"user_id": "u1"}},
                                    {"text": "You have CHK-1."}]})
    p = platform(FakeService(base_config()), tmp_path, llm=llm, dashboard_transport=httpx.MockTransport(traces))
    result = await p.run_agent("support_agent", "What are my accounts?", session_id="chat-7")
    assert posted == []  # queued, not sent yet (batch of 20, flush every 5 s)
    await p.close()
    (rec,) = posted
    assert rec["trace_id"] == result.trace_id and rec["run_id"] == result.run_id
    assert (rec["app"], rec["agent"], rec["session_id"], rec["bundle_revision"]) == (
        "ironclad", "ironclad.support_agent", "chat-7", 1)
    assert rec["status"] == "success" and rec["model"] == "gpt-test" and rec["prompt_version"] == "v3"
    assert rec["token_input"] == 20 and rec["token_output"] == 10
    assert [c["tool"] for c in rec["tool_calls"]] == ["get_accounts"]
    assert [s["type"] for s in rec["steps"]] == ["agent_start", "tool_call", "tool_result", "agent_end"]
    assert rec["content"] is None and "What are my accounts" not in json.dumps(rec)
    # the local copy still has everything
    (local,) = [s for s in p._exporter.sinks if isinstance(s, LocalSink)][0].read(result.trace_id)
    assert local["input"] == "What are my accounts?"


def test_dashboard_sink_is_exported():
    assert DashboardSink.__module__ == "bastion.trace"
