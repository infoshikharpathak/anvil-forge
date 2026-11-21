"""AnvilHooks: Anvil's agent checks as Agent Forge hooks.

    before_agent_run -> anvil.check_agent_input
    after_agent_run  -> anvil.check_agent_output
    on_handoff       -> anvil.check_handoff

An Anvil block becomes a HookDecision deny carrying Anvil's correction
message, so the same text shows up as the blocked agent's output and in
the trace's hook denials. Agent Forge treats a raising hook as a deny, so
this fails closed even if Anvil itself errors.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_forge.core.context import use_run_context
from agent_forge.core.hooks import AgentHooks, HookDecision

from .agents import anvil_agent_id

if TYPE_CHECKING:
    from agent_forge.core.context import RunContext
    from anvil import Anvil
    from anvil.models import AgentCheckResult


def _decision(result: AgentCheckResult) -> HookDecision:
    if result.allowed:
        return HookDecision.allow()
    correction = result.correction
    message = correction.message if correction else "Blocked by Anvil."
    if correction is not None:
        message = f"[{correction.violation.value}] {message}"
    return HookDecision.deny(message)


class AnvilHooks(AgentHooks):
    def __init__(self, anvil: Anvil, app: str) -> None:
        self._anvil = anvil
        self._app = app

    def _id(self, name: str | None) -> str:
        # Unregistered (e.g. planner-spawned) agents get an ID Anvil can never
        # match, so they hit the unknown_agent hard block. See agents.py.
        return anvil_agent_id(name, self._app, self._anvil.policy_engine.policy.agents)

    # Each check runs with ctx set as the current RunContext. Graph handoffs
    # fire in the router, outside any agent run, so without this the
    # classifier's scan of a handoff payload would not be attributed to the run.

    async def before_agent_run(self, ctx: RunContext, task: str) -> HookDecision:
        with use_run_context(ctx):
            return _decision(
                await self._anvil.check_agent_input(self._id(ctx.agent_name), task, trace_id=ctx.trace_id)
            )

    async def after_agent_run(self, ctx: RunContext, output: str) -> HookDecision:
        with use_run_context(ctx):
            return _decision(
                await self._anvil.check_agent_output(self._id(ctx.agent_name), output, trace_id=ctx.trace_id)
            )

    async def on_handoff(
        self, ctx: RunContext, from_agent: str, to_agent: str, payload: str
    ) -> HookDecision:
        with use_run_context(ctx):
            return _decision(
                await self._anvil.check_handoff(
                    self._id(from_agent), self._id(to_agent), payload, trace_id=ctx.trace_id
                )
            )
