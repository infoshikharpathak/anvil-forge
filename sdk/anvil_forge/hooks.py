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

from agent_forge.core.hooks import AgentHooks, HookDecision

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
    def __init__(self, anvil: Anvil) -> None:
        self._anvil = anvil

    async def before_agent_run(self, ctx: RunContext, task: str) -> HookDecision:
        return _decision(await self._anvil.check_agent_input(ctx.agent_name or "", task, trace_id=ctx.trace_id))

    async def after_agent_run(self, ctx: RunContext, output: str) -> HookDecision:
        return _decision(await self._anvil.check_agent_output(ctx.agent_name or "", output, trace_id=ctx.trace_id))

    async def on_handoff(
        self, ctx: RunContext, from_agent: str, to_agent: str, payload: str
    ) -> HookDecision:
        return _decision(
            await self._anvil.check_handoff(from_agent, to_agent, payload, trace_id=ctx.trace_id)
        )
