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

from typing import TYPE_CHECKING, Any

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
    def __init__(self, anvil: Anvil, app: str, on_output_check: Any = None) -> None:
        self._anvil = anvil
        # (run_id, info) -> None: what the output check decided, for the
        # platform (block or flag, which check, label, reason).
        self._on_output_check = on_output_check
        self._app = app
        # The task each agent run started with, so the output check can tell
        # the classifier what the user asked (keyed by run and agent).
        self._tasks: dict[tuple[str, str | None], str] = {}

    def _id(self, name: str | None) -> str:
        # Unregistered (e.g. planner-spawned) agents get an ID Anvil can never
        # match, so they hit the unknown_agent hard block. See agents.py.
        return anvil_agent_id(name, self._app, self._anvil.policy_engine.policy.agents)

    # Each check runs with ctx set as the current RunContext. Graph handoffs
    # fire in the router, outside any agent run, so without this the
    # classifier's scan of a handoff payload would not be attributed to the run.

    async def before_agent_run(self, ctx: RunContext, task: str) -> HookDecision:
        self._tasks[(ctx.run_id, ctx.agent_name)] = task
        with use_run_context(ctx):
            return _decision(
                await self._anvil.check_agent_input(self._id(ctx.agent_name), task, trace_id=ctx.trace_id)
            )

    async def after_agent_run(self, ctx: RunContext, output: str) -> HookDecision:
        task = self._tasks.pop((ctx.run_id, ctx.agent_name), None)
        with use_run_context(ctx):
            result = await self._anvil.check_agent_output(
                self._id(ctx.agent_name), output, trace_id=ctx.trace_id, user_request=task
            )
        if self._on_output_check is not None and (result.flagged or not result.allowed):
            self._on_output_check(ctx.run_id, {
                "check": result.check, "label": result.label,
                "result": "flag" if result.allowed else "block",
                "reason": result.reason or (result.correction.message if result.correction else None),
            })
        return _decision(result)

    async def on_handoff(
        self, ctx: RunContext, from_agent: str, to_agent: str, payload: str
    ) -> HookDecision:
        with use_run_context(ctx):
            return _decision(
                await self._anvil.check_handoff(
                    self._id(from_agent), self._id(to_agent), payload, trace_id=ctx.trace_id
                )
            )
