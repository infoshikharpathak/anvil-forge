"""Item 2: AnvilToolSource routes Agent Forge tool calls through anvil.execute."""

import json

import pytest
from agent_forge.core.context import RunContext
from anvil import Anvil

from anvil_forge.config import POLICY_NAME, load_config
from anvil_forge.tool_source import AnvilToolSource

from .conftest import base_config
from .upstream import Upstream


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv("IRONCLAD_TOKEN", "tok-123")
    cfg = load_config(base_config())
    anvil = Anvil(cfg.anvil, POLICY_NAME, app="ironclad", session_id="s")
    upstream = Upstream()
    anvil._transport = upstream.transport()
    seen = []
    source = AnvilToolSource(anvil, "ironclad", observer=lambda *a: seen.append(a))
    return anvil, upstream, source, seen


def ctx(agent="ironclad.support_agent", trace="T-1"):
    return RunContext(trace_id=trace, run_id="R-1", agent_name=agent)


async def test_lists_own_and_granted_tools_with_app_relative_names(setup):
    _, _, source, _ = setup
    specs = {s.name: s for s in await source.list_tools()}
    assert set(specs) == {"get_accounts", "initiate_transfer", "close_account", "notify__send_email"}
    assert specs["get_accounts"].parameters_json_schema["required"] == ["user_id"]
    assert specs["notify__send_email"].description == "Email a customer."


async def test_allowed_call_executes_through_anvil(setup):
    anvil, upstream, source, seen = setup
    out = json.loads(await source.call("get_accounts", {"user_id": "u1"}, ctx()))
    assert out["success"] is True and out["data"]["accounts"][0]["id"] == "CHK-1"
    assert upstream.requests[0].headers["authorization"] == "Bearer tok-123"  # Anvil credential injection
    (record,) = anvil.call_log.calls_for("s", "ironclad.support_agent")
    assert record.tool == "ironclad.get_accounts" and record.trace_id == "T-1"
    assert seen[0][1:3] == ("get_accounts", "ironclad.get_accounts")


async def test_disallowed_call_returns_correction_not_exception(setup):
    _, upstream, source, _ = setup
    out = json.loads(await source.call("close_account", {}, ctx()))
    assert out["success"] is False and out["tool_executed"] is False
    assert out["correction"]["violation"] == "disallowed_tool"
    assert "not permitted" in out["correction"]["message"]
    assert upstream.requests == []


async def test_sequence_violation_tells_agent_what_to_do(setup):
    _, _, source, _ = setup
    out = json.loads(await source.call("initiate_transfer", {"user_id": "u1", "amount": 5}, ctx()))
    assert out["correction"]["violation"] == "missing_prerequisite"
    assert out["correction"]["required_actions"] == ["get_accounts"]


async def test_granted_cross_app_tool_via_app__tool_name(setup):
    _, upstream, source, _ = setup
    out = json.loads(await source.call("notify__send_email", {"to": "a@b", "body": "hi"}, ctx("ironclad.resolver")))
    assert out["success"] is True and upstream.paths() == ["/email"]


async def test_unknown_tool_name_is_an_error_result(setup):
    _, _, source, seen = setup
    out = json.loads(await source.call("teleport", {}, ctx()))
    assert out["success"] is False and out["correction"]["violation"] == "tool_error"
    assert seen  # observer still told


async def test_broken_observer_never_breaks_the_call(setup):
    anvil, _, _, _ = setup

    def boom(*a):
        raise RuntimeError("observer down")

    source = AnvilToolSource(anvil, "ironclad", observer=boom)
    out = json.loads(await source.call("get_accounts", {"user_id": "u1"}, ctx()))
    assert out["success"] is True
