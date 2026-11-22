"""SDK surface for apps building their own views: prepare_tool_call,
session_records, session_prerequisites, describe_agent."""

import json

import httpx
import pytest

from bastion import Platform, PlatformError

from .conftest import BASE, base_config
from .llm import ScriptedOpenAI
from .upstream import Upstream

SUPPORT = BASE["agents"]["ironclad.support_agent"]["instructions"]


@pytest.fixture(autouse=True)
def _clean_registry():
    yield
    from agent_forge.tools import _REGISTRY, sources

    for name in [n for n, fn in _REGISTRY.items() if getattr(fn, "_tool_source", None) is not None]:
        _REGISTRY.pop(name)
    sources._SOURCE_TOOLS.clear()


def make(monkeypatch, steps, config=None, **kw):
    monkeypatch.setenv("IRONCLAD_TOKEN", "tok")
    llm = ScriptedOpenAI({SUPPORT: steps})
    upstream = Upstream()
    p = Platform.from_config(config or base_config(), app="ironclad", openai_client=llm,
                             http_transport=upstream.transport(), **kw)
    return p, upstream


async def test_prepare_tool_call_overrides_params_before_anvil(monkeypatch):
    seen = []

    def prepare(ctx, name, params):
        seen.append((ctx.agent_name, name, dict(params)))
        return {**params, "user_id": "signed-in-user"}  # the model doesn't get to choose

    p, upstream = make(monkeypatch, [
        {"tool": "get_accounts", "args": {"user_id": "someone-else"}},
        {"tool": "initiate_transfer", "args": {"user_id": "someone-else", "amount": 5}},
        {"text": "done"},
    ], prepare_tool_call=prepare)
    reply = await p.run_agent("support_agent", "send $5", session_id="s1")
    assert seen[0] == ("ironclad.support_agent", "get_accounts", {"user_id": "someone-else"})
    assert upstream.requests[0].url.params["user_id"] == "signed-in-user"
    # Anvil saw the prepared value, so match_fields(user_id) lines up and the transfer is allowed
    assert [c["params"]["user_id"] for c in reply.trace["tool_calls"]] == ["signed-in-user"] * 2
    assert reply.trace["tool_calls"][1]["success"] is True


async def test_broken_prepare_is_a_tool_error_not_a_crash(monkeypatch):
    def prepare(ctx, name, params):
        raise KeyError("no session user")

    p, upstream = make(monkeypatch, [{"tool": "get_accounts", "args": {"user_id": "u"}}, {"text": "sorry"}],
                       prepare_tool_call=prepare)
    reply = await p.run_agent("support_agent", "accounts")
    assert reply.trace["tool_calls"][0]["violation"] == "tool_error" and upstream.requests == []


async def test_session_records_and_prerequisites(monkeypatch):
    p, _ = make(monkeypatch, [
        {"tool": "close_account", "args": {}},
        {"tool": "get_accounts", "args": {"user_id": "u1"}},
        {"text": "done"},
    ])
    assert p.session_records("chat") == [] and p.session_prerequisites("chat", "support_agent") == []
    await p.run_agent("support_agent", "accounts", session_id="chat", trace_id="t1")
    records = p.session_records("chat")
    tools = [r for r in records if r["kind"] == "tool"]
    assert [(r["tool"], r["result"], r["violation"]) for r in tools] == [
        ("ironclad.close_account", "blocked", "disallowed_tool"),
        ("ironclad.get_accounts", "success", None),
    ]
    assert {r["kind"] for r in records} == {"agent_input", "tool", "agent_output"}
    assert all(r["trace_id"] == "t1" for r in records)
    assert tools[1]["params"] == {"user_id": "u1"} and tools[1]["latency_ms"] is not None
    assert p.session_prerequisites("chat", "support_agent") == ["get_accounts"]
    with pytest.raises(PlatformError):
        p.session_prerequisites("chat", "notify.dispatcher")


async def test_session_records_are_per_session(monkeypatch):
    p, _ = make(monkeypatch, [{"tool": "get_accounts", "args": {"user_id": "u1"}}, {"text": "a"}, {"text": "b"}])
    await p.run_agent("support_agent", "accounts", session_id="A")
    await p.run_agent("support_agent", "hello", session_id="B")
    assert [r["tool"] for r in p.session_records("A") if r["kind"] == "tool"] == ["ironclad.get_accounts"]
    assert [r for r in p.session_records("B") if r["kind"] == "tool"] == []
    assert p.session_prerequisites("B", "support_agent") == []
    await p.end_session("A")
    assert p.session_records("A") == []


def test_describe_agent(monkeypatch):
    cfg = base_config()
    cfg["agents"]["ironclad.support_agent"]["description"] = "Support agents look before they transfer."
    cfg["agents"]["ironclad.support_agent"]["trust_overrides"] = {"get_accounts": "untrusted"}
    p, _ = make(monkeypatch, [], config=cfg)
    d = p.describe_agent("support_agent")
    assert d["id"] == "ironclad.support_agent" and d["description"].startswith("Support agents")
    assert d["strictness"] == "strict" and d["prompt_version"] == "v3"
    assert d["tools"] == [
        {"name": "get_accounts", "trust": "untrusted", "injection_scanning": True, "description": "List the user's accounts."},  # override wins
        {"name": "initiate_transfer", "trust": "untrusted", "injection_scanning": True, "description": "Start a transfer."},
    ]
    assert d["sequences"] == [{"before": "initiate_transfer", "requires": ["get_accounts"], "match_fields": ["user_id"]}]
    assert d["settings"] == {"injection_scanning": True, "default_trust": "untrusted", "session_ttl": 3600}
    assert p.describe_agent("resolver")["tools"][0]["name"] == "notify__send_email"


def test_trust_overrides_compile_into_policy_and_are_validated(monkeypatch):
    from bastion import ConfigError, load_config
    from bastion.config import POLICY_NAME

    cfg = base_config()
    cfg["agents"]["ironclad.support_agent"]["trust_overrides"] = {"get_accounts": "untrusted"}
    compiled = load_config(cfg)
    assert compiled.anvil.policies[POLICY_NAME].agents["ironclad.support_agent"].trust_overrides == {"get_accounts": "untrusted"}
    cfg["agents"]["ironclad.support_agent"]["trust_overrides"] = {"close_account": "trusted"}
    with pytest.raises(ConfigError, match="trust_overrides references 'ironclad.close_account' not in allowed_tools"):
        load_config(cfg)


async def test_prepare_can_add_a_host_only_param_the_model_cannot_send(monkeypatch):
    """A param prepare adds that the tool's schema doesn't declare goes
    through (it's the host's); the same param sent by the model is blocked."""
    def prepare(ctx, name, params):
        return {**params, "user_id": "signed-in-user", "channel": "agent"}

    p, upstream = make(monkeypatch, [
        {"tool": "get_accounts", "args": {"user_id": "u"}},
        {"tool": "initiate_transfer", "args": {"amount": 5}},
        {"tool": "initiate_transfer", "args": {"amount": 5, "channel": "branch"}},
        {"text": "done"},
    ], prepare_tool_call=prepare)
    reply = await p.run_agent("support_agent", "send $5", session_id="s-host")
    calls = reply.trace["tool_calls"]
    assert [c["success"] for c in calls] == [True, True, False]
    assert calls[2]["violation"] == "invalid_params" and "channel: unknown field" in calls[2]["correction"]
    sent = json.loads(upstream.requests[1].content)
    assert sent == {"user_id": "signed-in-user", "amount": 5, "channel": "agent"}
    assert len(upstream.requests) == 2  # the model's channel never left


async def test_upstream_error_body_reaches_the_model_and_the_records(monkeypatch):
    class Refusing(Upstream):
        def __call__(self, request):
            if request.url.path == "/transfers":
                self.requests.append(request)
                return httpx.Response(403, json={"error": "LIMIT", "message": "Over the daily limit."})
            return super().__call__(request)

    monkeypatch.setenv("IRONCLAD_TOKEN", "tok")
    steps = [{"tool": "get_accounts", "args": {"user_id": "u"}},
             {"tool": "initiate_transfer", "args": {"user_id": "u", "amount": 5}}, {"text": "over the limit"}]
    llm = ScriptedOpenAI({SUPPORT: steps})
    p = Platform.from_config(base_config(), app="ironclad", openai_client=llm, http_transport=Refusing().transport())
    reply = await p.run_agent("support_agent", "send $5", session_id="s-err")
    (_, refused) = reply.trace["tool_calls"]
    assert refused["success"] is False and refused["violation"] == "upstream_error"
    assert refused["response"] == {"http_status": 403, "body": {"error": "LIMIT", "message": "Over the daily limit."}}
    (record,) = [r for r in p.session_records("s-err") if r["tool"] == "ironclad.initiate_transfer"]
    assert (record["result"], record["violation"], record["http_status"]) == ("blocked", "upstream_error", 403)
