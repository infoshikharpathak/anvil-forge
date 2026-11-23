"""Agent-to-agent: remote_agent tools, caller identity from keys, linked traces."""

import json

import httpx
import pytest

from garrison import Platform
from garrison.a2a import CALL_CHAIN_HEADER, TRACE_ID_HEADER, CallerKeys, hash_key, new_key, parse_chain
from garrison.trace import dashboard_record

from .llm import ScriptedOpenAI

HUB_KEY = "ak_hub_issued_by_fooddash"
HUB = "You are the chat hub."


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("HUB_FOODDASH_KEY", HUB_KEY)
    yield
    from agent_forge.tools import _REGISTRY, sources

    for name in [n for n, fn in _REGISTRY.items() if getattr(fn, "_tool_source", None) is not None]:
        _REGISTRY.pop(name)
    sources._SOURCE_TOOLS.clear()


def config():
    return {
        "apps": {
            "fooddash": {"base_url": "http://food.test", "auth": {"header": "Authorization", "prefix": "Bearer "},
                         "tools": {"ask_fooddash": {"type": "remote_agent", "description": "Ask FoodDash's agent."}}},
            "chathub": {"credentials": {"fooddash": "env:HUB_FOODDASH_KEY"}, "tools": {}},
        },
        "agents": {"chathub.assistant": {"instructions": HUB, "prompt_version": "v1", "model": "gpt-test",
                                         "tools": ["fooddash.ask_fooddash"]}},
        "grants": [{"tool": "fooddash.ask_fooddash", "to_apps": ["chathub"]}],
        "classifier": {"enabled": False},
    }


class Food:
    def __init__(self):
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        return httpx.Response(200, json={"response": "Golden Dragon and Pasta Palace.",
                                         "session_id": json.loads(request.content)["session_id"],
                                         "trace_id": "child-trace-1"})


async def ask(steps, **run_kwargs):
    food = Food()
    llm = ScriptedOpenAI({HUB: steps})
    p = Platform.from_config(config(), app="chathub", openai_client=llm, http_transport=httpx.MockTransport(food),
                             sinks=[])
    reply = await p.run_agent("assistant", "What restaurants are there?", session_id="s1", **run_kwargs)
    return reply, food, llm


ASK = {"tool": "fooddash__ask_fooddash", "args": {"message": "What restaurants are available?"}}


async def test_remote_agent_call_sends_credential_chain_trace_and_session():
    reply, food, _ = await ask([ASK, {"text": "Golden Dragon and Pasta Palace."}])
    (req,) = food.requests
    assert (req.method, req.url.path) == ("POST", "/agent/chat")
    assert json.loads(req.content) == {"message": "What restaurants are available?", "session_id": "chathub-s1"}
    assert req.headers["authorization"] == f"Bearer {HUB_KEY}"
    assert req.headers[CALL_CHAIN_HEADER] == "chathub.assistant"
    assert req.headers[TRACE_ID_HEADER] == reply.trace_id
    assert reply.trace["chain"] == ["chathub.assistant"] and reply.trace["caller_app"] is None
    assert reply.trace["child_traces"] == [{"app": "fooddash", "trace_id": "child-trace-1"}]


async def test_the_model_cannot_choose_the_remote_session():
    steps = [{"tool": "fooddash__ask_fooddash", "args": {"message": "hi", "session_id": "victim-session"}},
             {"text": "ok"}]
    reply, food, llm = await ask(steps)
    assert food.requests == []
    (call,) = reply.trace["tool_calls"]
    assert call["violation"] == "invalid_params"


async def test_callee_side_trace_fields_and_run_context_app():
    seen = []

    def prepare(ctx, tool, params):
        seen.append(ctx.app)
        return params

    llm = ScriptedOpenAI({HUB: [ASK, {"text": "ok"}]})
    p = Platform.from_config(config(), app="chathub", openai_client=llm, http_transport=httpx.MockTransport(Food()),
                             sinks=[], prepare_tool_call=prepare)
    reply = await p.run_agent("assistant", "hi", session_id="s2", caller_app="portal",
                              parent_trace_id="parent-123", chain=["portal.front"])
    assert reply.trace["caller_app"] == "portal" and reply.trace["parent_trace_id"] == "parent-123"
    assert reply.trace["chain"] == ["portal.front", "chathub.assistant"]
    assert seen == ["chathub"]
    rec = dashboard_record(reply.trace)
    assert (rec["caller_app"], rec["parent_trace_id"], rec["chain"]) == (
        "portal", "parent-123", ["portal.front", "chathub.assistant"])
    assert rec["child_traces"] == [{"app": "fooddash", "trace_id": "child-trace-1"}]


def test_caller_identity_comes_from_the_key_not_the_header():
    key = new_key()
    keys = CallerKeys({hash_key(key): {"app": "chathub", "user_id": "user_1"}})
    forged = {"Authorization": f"Bearer {key}", CALL_CHAIN_HEADER: "fooddash.ordering_agent",
              TRACE_ID_HEADER: "abc-123"}
    caller = keys.authenticate(forged)
    assert caller.app == "chathub" and caller.user_id == "user_1"
    assert caller.chain == ["fooddash.ordering_agent"] and caller.parent_trace_id == "abc-123"


@pytest.mark.parametrize("headers", [
    {}, {"Authorization": "Bearer ak_unknown"}, {"Authorization": "Basic abc"},
    {"Authorization": "Bearer pk_not_an_agent_key"}, {CALL_CHAIN_HEADER: "chathub.assistant"},
])
def test_unknown_or_missing_key_is_no_caller(headers):
    keys = CallerKeys({hash_key(new_key()): {"app": "chathub"}})
    assert keys.authenticate(headers) is None


def test_parse_chain_drops_malformed_hops():
    assert parse_chain("chathub.assistant, bad hop,<x>.y, a.b") == ["chathub.assistant", "a.b"]
    assert len(parse_chain(",".join(["a.b"] * 50))) == 10


def test_remote_agent_with_a_full_endpoint_compiles():
    """An agent service on its own host: endpoint replaces the default path
    (Anvil rejects a tool with both)."""
    from garrison.config import load_config

    cfg = {
        "apps": {
            "hub": {"credentials": {"shop": "env:HUB_SHOP_KEY"}},
            "shop": {"base_url": "http://shop.test:8000", "auth": {"header": "Authorization", "prefix": "Bearer "},
                     "tools": {"ask_shop": {"type": "remote_agent", "endpoint": "http://shop-agent.test:8100/agent/chat"}}},
        },
        "grants": [{"tool": "shop.ask_shop", "to_apps": ["hub"]}],
        "agents": {"hub.assistant": {"instructions": "x", "prompt_version": "v1", "tools": ["shop.ask_shop"]}},
    }
    tool = load_config(cfg).anvil_for("hub").tools["shop.ask_shop"]
    assert tool.endpoint == "http://shop-agent.test:8100/agent/chat" and tool.path is None
    cfg["apps"]["shop"]["tools"]["ask_shop"] = {"type": "remote_agent"}
    assert load_config(cfg).anvil_for("hub").tools["shop.ask_shop"].endpoint == "http://shop.test:8000/agent/chat"
