"""session_id: Anvil state and agent memory carried across turns."""

import asyncio

import pytest

from garrison import LocalSink, Platform

from .conftest import BASE, base_config
from .llm import ScriptedOpenAI
from .upstream import Upstream

SUPPORT = BASE["agents"]["ironclad.support_agent"]["instructions"]
TRIAGE = BASE["agents"]["ironclad.triage"]["instructions"]
RESOLVER = BASE["agents"]["ironclad.resolver"]["instructions"]

LOOKUP = [{"tool": "get_accounts", "args": {"user_id": "u1"}}, {"text": "You have CHK-1."}]
TRANSFER = [{"tool": "initiate_transfer", "args": {"user_id": "u1", "amount": 50}}, {"text": "Transfer started."}]


@pytest.fixture(autouse=True)
def _clean_registry():
    yield
    from agent_forge.tools import _REGISTRY, sources

    for name in [n for n, fn in _REGISTRY.items() if getattr(fn, "_tool_source", None) is not None]:
        _REGISTRY.pop(name)
    sources._SOURCE_TOOLS.clear()


def make(monkeypatch, steps, **clients):
    monkeypatch.setenv("IRONCLAD_TOKEN", "tok")
    llm = ScriptedOpenAI({SUPPORT: steps})
    upstream = Upstream()
    p = Platform.from_config(base_config(), app="ironclad", openai_client=llm,
                             http_transport=upstream.transport(), **clients)
    return p, llm, upstream


async def test_sequence_rule_spans_turns_in_a_session(monkeypatch):
    p, _, upstream = make(monkeypatch, LOOKUP + TRANSFER)
    await p.run_agent("support_agent", "What are my accounts?", session_id="chat-1")
    second = await p.run_agent("support_agent", "Send $50", session_id="chat-1")
    (call,) = second.trace["tool_calls"]
    assert call["success"] is True and call["response"]["transfer_id"] == "T-9"
    assert upstream.paths() == ["/accounts", "/transfers"]
    assert second.session_id == "chat-1" and second.trace["session_id"] == "chat-1"


async def test_without_a_session_each_turn_starts_clean(monkeypatch):
    p, _, upstream = make(monkeypatch, LOOKUP + TRANSFER)
    await p.run_agent("support_agent", "What are my accounts?")
    second = await p.run_agent("support_agent", "Send $50")
    assert second.trace["tool_calls"][0]["violation"] == "missing_prerequisite"
    assert "/transfers" not in upstream.paths()
    assert second.session_id is None and p.list_sessions() == []


async def test_agent_remembers_the_conversation(monkeypatch):
    p, llm, _ = make(monkeypatch, LOOKUP + [{"text": "As I said, CHK-1."}])
    await p.run_agent("support_agent", "What are my accounts?", session_id="chat-2")
    await p.run_agent("support_agent", "Remind me?", session_id="chat-2")
    turn2 = llm.requests[SUPPORT][-1]["messages"]
    contents = [m.get("content") if isinstance(m, dict) else None for m in turn2]
    assert "What are my accounts?" in contents and "You have CHK-1." in contents and "Remind me?" in contents


async def test_sessions_are_isolated_from_each_other(monkeypatch):
    p, llm, upstream = make(monkeypatch, LOOKUP + TRANSFER)
    await p.run_agent("support_agent", "What are my accounts?", session_id="alice")
    other = await p.run_agent("support_agent", "Send $50", session_id="bob")
    assert other.trace["tool_calls"][0]["violation"] == "missing_prerequisite"
    assert "What are my accounts?" not in str(llm.requests[SUPPORT][-1]["messages"])
    assert sorted(p.list_sessions()) == ["alice", "bob"]


async def test_each_turn_gets_its_own_trace_even_with_a_shared_trace_id(monkeypatch):
    p, _, _ = make(monkeypatch, LOOKUP + TRANSFER)
    first = await p.run_agent("support_agent", "accounts", session_id="s", trace_id="conv-1")
    second = await p.run_agent("support_agent", "send $50", session_id="s", trace_id="conv-1")
    assert first.run_id != second.run_id
    tools = [r["tool"] for r in second.trace["anvil_records"] if r["kind"] == "tool"]
    assert tools == ["ironclad.initiate_transfer"]  # turn 1's get_accounts not repeated
    assert [t["run_id"] for t in LocalSink().read("conv-1")] == [first.run_id, second.run_id]


async def test_concurrent_turns_in_one_session_are_serialized(monkeypatch):
    p, llm, _ = make(monkeypatch, [{"text": "first answer"}, {"text": "second answer"}])
    a, b = await asyncio.gather(
        p.run_agent("support_agent", "one", session_id="s"),
        p.run_agent("support_agent", "two", session_id="s"),
    )
    assert {a.content, b.content} == {"first answer", "second answer"}
    # the second turn the model saw contains the whole first exchange, in order
    last = [m["content"] for m in llm.requests[SUPPORT][-1]["messages"] if isinstance(m, dict)]
    assert last[1:] in (["one", "first answer", "two"], ["two", "first answer", "one"])


async def test_end_session_forgets_state_and_memory(monkeypatch):
    p, llm, _ = make(monkeypatch, LOOKUP + TRANSFER)
    await p.run_agent("support_agent", "What are my accounts?", session_id="chat")
    assert await p.end_session("chat") is True and await p.end_session("chat") is False
    again = await p.run_agent("support_agent", "Send $50", session_id="chat")
    assert again.trace["tool_calls"][0]["violation"] == "missing_prerequisite"
    assert "What are my accounts?" not in str(llm.requests[SUPPORT][-1]["messages"])


async def test_idle_sessions_expire(monkeypatch):
    p, _, _ = make(monkeypatch, LOOKUP + TRANSFER)
    await p.run_agent("support_agent", "What are my accounts?", session_id="old")
    p._session_ttl = -1  # everything idle is now past its TTL
    await p.run_agent("support_agent", "Send $50", session_id="new")
    assert p.list_sessions() == ["new"]


async def test_flow_shares_the_session_anvil(monkeypatch):
    monkeypatch.setenv("IRONCLAD_TOKEN", "tok")
    from .llm import ScriptedAnthropic

    oa = ScriptedOpenAI({TRIAGE: [{"text": "Duplicate charge on order 4471, routing to the resolver."}]})
    an = ScriptedAnthropic({RESOLVER: [{"text": "Refund issued for the duplicate charge on order 4471."}]})
    p = Platform.from_config(base_config(), app="ironclad", openai_client=oa, anthropic_client=an,
                             http_transport=Upstream().transport())
    result = await p.run_flow("dispute_flow", "charged twice", session_id="case-9")
    assert result.session_id == "case-9" and result.trace["session_id"] == "case-9"
    session_anvil = p._sessions["case-9"].anvil
    assert session_anvil.call_log.calls_for(session_anvil.session_id, "ironclad.triage")
