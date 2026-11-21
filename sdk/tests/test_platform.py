"""Item 7: the public Platform API."""

import asyncio

import pytest
import yaml

from bastion import LocalSink, Platform, PlatformError

from .conftest import BASE, base_config
from .llm import ScriptedAnthropic, ScriptedOpenAI
from .upstream import Upstream

SUPPORT = BASE["agents"]["ironclad.support_agent"]["instructions"]
TRIAGE = BASE["agents"]["ironclad.triage"]["instructions"]
RESOLVER = BASE["agents"]["ironclad.resolver"]["instructions"]


@pytest.fixture
def make(monkeypatch):
    monkeypatch.setenv("IRONCLAD_TOKEN", "tok")

    def _make(openai=None, anthropic=None, config=None, **kw):
        upstream = Upstream()
        p = Platform.from_config(config or base_config(), app="ironclad",
                                 openai_client=openai, anthropic_client=anthropic,
                                 http_transport=upstream.transport(), **kw)
        return p, upstream

    return _make


@pytest.fixture(autouse=True)
async def _close_platforms():
    yield
    # unregister tool sources between tests
    from agent_forge.tools import _REGISTRY
    for name in [n for n, fn in _REGISTRY.items() if getattr(fn, "_tool_source", None) is not None]:
        _REGISTRY.pop(name)
    from agent_forge.tools import sources
    sources._SOURCE_TOOLS.clear()


def test_introspection_uses_bare_names(make):
    p, _ = make()
    agents = {a["name"]: a for a in p.list_agents()}
    assert set(agents) == {"support_agent", "triage", "resolver"}  # not notify.dispatcher
    assert agents["resolver"] == {
        "name": "resolver", "id": "ironclad.resolver", "prompt_version": "v2",
        "backend": "anthropic", "model": "claude-test", "tools": ["notify__send_email"],
    }
    assert sorted(p.list_tools()) == ["close_account", "get_accounts", "initiate_transfer", "notify__send_email"]
    assert p.list_flows() == ["dispute_flow"]


def test_from_yaml_path(make, tmp_path):
    path = tmp_path / "bastion.yaml"
    path.write_text(yaml.safe_dump(base_config()))
    p, _ = make(config=str(path))
    assert p.list_flows() == ["dispute_flow"]


def test_unknown_app_rejected():
    with pytest.raises(PlatformError, match="App 'ghost'"):
        Platform.from_config(base_config(), app="ghost")


async def test_run_agent_qualifies_bare_name_and_traces(make):
    llm = ScriptedOpenAI({SUPPORT: [{"tool": "get_accounts", "args": {"user_id": "u1"}}, {"text": "You have CHK-1."}]})
    p, upstream = make(openai=llm)
    reply = await p.run_agent("support_agent", "What are my accounts?", trace_id="req-1")
    assert str(reply) == "You have CHK-1." and reply.status == "success"
    assert reply.agent == "ironclad.support_agent" and reply.trace_id == "req-1"
    assert upstream.paths() == ["/accounts"]
    assert reply.trace["agents"][0]["agent"] == "ironclad.support_agent"
    (row,) = LocalSink().read("req-1")  # default LocalSink, in the app's cwd
    assert row["run_id"] == reply.run_id


async def test_names_outside_the_bound_app_rejected(make):
    p, _ = make()
    with pytest.raises(PlatformError, match="Unknown agent 'nobody'"):
        await p.run_agent("nobody", "hi")
    with pytest.raises(PlatformError, match="belongs to app 'notify'"):
        await p.run_agent("notify.dispatcher", "hi")
    with pytest.raises(PlatformError, match="Unknown flow"):
        await p.run_flow("support_agent", "hi")


async def test_run_flow_runs_graph_with_handoff(make):
    oa = ScriptedOpenAI({TRIAGE: [{"text": "Duplicate charge on order 4471. Route to resolver."}]})
    an = ScriptedAnthropic({RESOLVER: [{"tool": "notify__send_email", "args": {"to": "c@x", "body": "Refunded."}}, {"text": "Refund issued for the duplicate charge and the customer has been emailed."}]})
    p, upstream = make(openai=oa, anthropic=an)
    result = await p.run_flow("dispute_flow", "I was charged twice.", trace_id="flow-1")
    assert [m["agent"] for m in result.messages] == ["ironclad.triage", "ironclad.resolver"]
    assert result.content.startswith("Refund issued") and result.stopped_by == "graph"
    assert upstream.paths() == ["/email"]
    handoffs = [r for r in result.trace["anvil_records"] if r["kind"] == "handoff"]
    assert handoffs[0]["agent"] == "ironclad.triage" and handoffs[0]["target_agent"] == "ironclad.resolver"
    assert "Duplicate charge" in an.requests[RESOLVER][0]["messages"][0]["content"]  # triage's work handed over


async def test_sequence_state_is_scoped_to_one_run(make):
    llm = ScriptedOpenAI({SUPPORT: [
        {"tool": "get_accounts", "args": {"user_id": "u1"}}, {"text": "done"},          # run 1
        {"tool": "initiate_transfer", "args": {"user_id": "u1", "amount": 5}}, {"text": "blocked?"},  # run 2
    ]})
    p, upstream = make(openai=llm)
    await p.run_agent("support_agent", "accounts")
    second = await p.run_agent("support_agent", "transfer $5")
    (call,) = second.trace["tool_calls"]
    assert call["violation"] == "missing_prerequisite"  # run 1's get_accounts doesn't count
    assert "/transfers" not in upstream.paths()


async def test_concurrent_runs_keep_separate_traces(make):
    def respond(kwargs):
        msgs = kwargs["messages"]
        user = next(m["content"] for m in msgs if isinstance(m, dict) and m.get("role") == "user")
        if any(isinstance(m, dict) and m.get("role") == "tool" for m in msgs):
            return {"text": f"done with {user}"}
        return {"tool": "get_accounts", "args": {"user_id": "a" if user == "first" else "b"}}

    llm = ScriptedOpenAI({SUPPORT: respond})
    p, _ = make(openai=llm)
    r1, r2 = await asyncio.gather(
        p.run_agent("support_agent", "first", trace_id="t-A"),
        p.run_agent("support_agent", "second", trace_id="t-B"),
    )
    for r in (r1, r2):
        assert {rec["trace_id"] for rec in r.trace["anvil_records"]} == {r.trace_id}
        assert len(r.trace["tool_calls"]) == 1 and r.trace["agents"][0]["input"] in ("first", "second")
    assert {r1.trace["tool_calls"][0]["params"]["user_id"], r2.trace["tool_calls"][0]["params"]["user_id"]} == {"a", "b"}


async def test_failing_sink_never_fails_the_run(make):
    from bastion import TraceSink

    class Broken(TraceSink):
        async def export(self, trace):
            raise RuntimeError("no disk")

    p, _ = make(openai=ScriptedOpenAI({SUPPORT: [{"text": "hello"}]}), sinks=[Broken()])
    assert (await p.run_agent("support_agent", "hi")).content == "hello"


async def test_llm_failure_is_traced_then_raised(make, tmp_path):
    p, _ = make(openai=ScriptedOpenAI({}))  # no scripted reply -> the client raises
    with pytest.raises(AssertionError, match="no scripted reply"):
        await p.run_agent("support_agent", "hi", trace_id="boom-1")
    (row,) = LocalSink().read("boom-1")
    assert row["outcome"] == "failed" and "no scripted reply" in row["error"]
