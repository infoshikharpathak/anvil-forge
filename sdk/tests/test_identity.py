"""Item 4: registered agents run as qualified app.agent; unregistered
(planner-spawned) agents are stopped by Anvil's unknown_agent hard block."""

import pytest
from agent_forge.core.conversation import ConversationMessage
from agent_forge.core.execution import execute_agent
from agent_forge.core.graph_runner import GraphRunner
from agent_forge.core.orchestrator import GraphNode, GraphSpec
from agent_forge.tools.sources import register_tool_source, unregister_tool_source
from anvil import Anvil

from anvil_forge.agents import ClientProvider, PlatformFactory, anvil_agent_id
from anvil_forge.config import POLICY_NAME, load_config
from anvil_forge.hooks import AnvilHooks
from anvil_forge.tool_source import AnvilToolSource

from .conftest import BASE, base_config
from .llm import ScriptedAnthropic, ScriptedOpenAI
from .upstream import Upstream

SUPPORT = BASE["agents"]["ironclad.support_agent"]["instructions"]
RESOLVER = BASE["agents"]["ironclad.resolver"]["instructions"]


@pytest.fixture
async def env(monkeypatch):
    monkeypatch.setenv("IRONCLAD_TOKEN", "tok")
    cfg = load_config(base_config())
    anvil = Anvil(cfg.anvil, POLICY_NAME, app="ironclad", session_id="s")
    upstream = Upstream()
    anvil._transport = upstream.transport()
    source = AnvilToolSource(anvil, "ironclad")
    await register_tool_source(source)
    yield cfg, anvil, upstream
    unregister_tool_source(source)


def test_anvil_agent_id_mapping():
    registered = {"ironclad.triage": 1}
    assert anvil_agent_id("ironclad.triage", "ironclad", registered) == "ironclad.triage"
    assert anvil_agent_id("triage", "ironclad", registered) == "ironclad.unregistered:triage"
    assert anvil_agent_id(None, "ironclad", registered) == "ironclad.unregistered:anonymous"


async def test_registered_agent_is_qualified_and_gets_policy_tools(env):
    cfg, anvil, upstream = env
    llm = ScriptedAnthropic({RESOLVER: [{"tool": "notify__send_email", "args": {"to": "a@b", "body": "done"}}, {"text": "Emailed."}]})
    factory = PlatformFactory(cfg, "ironclad", ClientProvider(anthropic=llm))
    agent = await factory.create(name="ironclad.resolver", system_message="planner text is ignored")
    assert agent.name == "ironclad.resolver"
    assert [t["name"] for t in agent._tools] == ["notify__send_email"]  # offered: policy only
    # handlers: every tool the app can see, so off-policy calls still reach Anvil
    assert set(agent._tool_handlers) == {"get_accounts", "initiate_transfer", "close_account", "notify__send_email"}
    assert agent._system_message == RESOLVER

    result = await execute_agent(agent, "resolve", agent.name, None, [AnvilHooks(anvil, "ironclad")])
    assert result.content == "Emailed." and upstream.paths() == ["/email"]
    (call,) = [c for c in anvil.call_log.calls_for("s", "ironclad.resolver") if c.kind == "tool"]
    assert call.agent == "ironclad.resolver" and call.tool == "notify.send_email"


@pytest.mark.parametrize("planner_name", ["researcher", "triage"])  # "triage" shadows a registered short name
async def test_planner_spawned_agent_blocked_as_unknown(env, planner_name):
    cfg, anvil, upstream = env
    llm = ScriptedOpenAI({})  # any model call would fail the test
    factory = PlatformFactory(cfg, "ironclad", ClientProvider(openai=llm))
    spec = GraphSpec(
        nodes=[GraphNode(name=planner_name, role_description="r", system_prompt="planned", task_prompt="t")],
        edges=[], entry=planner_name,
    )
    runner = GraphRunner(factory, spec, hooks=[AnvilHooks(anvil, "ironclad")])
    events = [e async for e in runner.run_stream("goal")]
    msg = next(e for e in events if isinstance(e, ConversationMessage))
    assert msg.content.startswith("[unknown_agent]")
    assert f"ironclad.unregistered:{planner_name}" in msg.content
    assert llm.requests == {} and upstream.requests == []


async def test_unregistered_agent_tool_call_blocked(env):
    from agent_forge.core.context import RunContext, use_run_context
    from agent_forge.tools import get_tool

    _, _, upstream = env
    with use_run_context(RunContext(trace_id="T", run_id="R", agent_name="researcher")):
        out = await get_tool("get_accounts")(user_id="u1")
    assert '"unknown_agent"' in out and upstream.requests == []
