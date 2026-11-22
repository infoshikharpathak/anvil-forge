"""AnvilToolSource: Anvil-governed tools exposed to Agent Forge agents.

Implements Agent Forge's ``ToolSource`` protocol. Every call goes through
``anvil.execute``, so allowed_tools, sequences, cross-app grants, injection
scans and credential injection all apply. A blocked call is not an
exception: Anvil's corrective response is returned to the model as the
tool result, so the agent can read it and self-correct (for example, call
the missing prerequisite first).
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any, Callable

from agent_forge.core.context import RunContext
from agent_forge.tools.sources import ToolSpec
from anvil.namespaces import relative, resolve

from .agents import anvil_agent_id

if TYPE_CHECKING:
    from anvil import Anvil

log = logging.getLogger(__name__)

_EMPTY_SCHEMA = {"type": "object", "properties": {}}

# (ctx, called_name, tool_id, args, response_dict, timing) -> None, where
# timing is {"started": perf_counter(), "duration_ms": float}.
ToolCallObserver = Callable[[RunContext, str, str, dict, dict, dict], None]

# (ctx, called_name, args) -> args. Lets the app fill in parameters the model
# must not choose (the signed-in user's ID, idempotency keys) before Anvil
# sees them, so Anvil's checks run on the final values.
ToolCallPreparer = Callable[[RunContext, str, dict], dict]


class AnvilToolSource:
    """*anvil* is the Anvil instance to execute through. Pass *anvil_for* to
    pick one per call from the RunContext instead (the Platform gives each
    run its own Anvil session); it may return None to fall back to *anvil*."""

    def __init__(
        self,
        anvil: Anvil,
        app: str,
        *,
        observer: ToolCallObserver | None = None,
        anvil_for: Callable[[RunContext], Anvil | None] | None = None,
        prepare: ToolCallPreparer | None = None,
    ) -> None:
        self._anvil = anvil
        self._app = app
        self._observer = observer
        self._anvil_for = anvil_for
        self._prepare = prepare

    def visible_tool_ids(self) -> list[str]:
        """The app's own tools plus tools granted to it, as canonical IDs."""
        engine = self._anvil.policy_engine
        return [
            tool_id
            for tool_id, cfg in self._anvil.config.tools.items()
            if cfg.app == self._app or engine.tool_granted(tool_id, self._app)
        ]

    async def list_tools(self) -> list[ToolSpec]:
        specs = []
        for tool_id in self.visible_tool_ids():
            cfg = self._anvil.config.tools[tool_id]
            specs.append(ToolSpec(
                name=relative(tool_id, self._app),  # bare for own app, app__tool otherwise
                description=cfg.description,
                parameters_json_schema=cfg.parameters or dict(_EMPTY_SCHEMA),
            ))
        return specs

    async def call(self, name: str, args: dict[str, Any], ctx: RunContext) -> str:
        tool_id = resolve(name, self._app)
        anvil = (self._anvil_for(ctx) if self._anvil_for else None) or self._anvil
        started = time.perf_counter()
        try:
            model_params, host_params = dict(args), {}
            if self._prepare is not None:
                prepared = dict(self._prepare(ctx, name, dict(args)))
                # What prepare added or changed is the host's, not the model's:
                # Anvil lets those through its schema check (a host-only field
                # the model didn't send may be undeclared). Anything the model
                # sent is checked as the model's, so it can't set such a field.
                host_params = {k: v for k, v in prepared.items() if k not in args or args[k] != v}
                model_params = {k: v for k, v in args.items() if k in prepared}
                args = prepared
            response = await anvil.execute(
                agent_id=anvil_agent_id(ctx.agent_name, self._app, anvil.policy_engine.policy.agents),
                tool_name=tool_id,
                params=model_params,
                host_params=host_params,
                trace_id=ctx.trace_id,
            )
            payload = response.model_dump(mode="json")
        except Exception as exc:  # never raise into the agent loop
            log.exception("[anvil_tool_source] %s failed", tool_id)
            payload = {
                "success": False,
                "data": None,
                "tool_executed": False,
                "correction": {
                    "violation": "tool_error",
                    "message": f"Tool {name!r} could not be run: {exc}",
                    "required_actions": [],
                    "call_history": [],
                },
            }
        timing = {"started": started, "duration_ms": round((time.perf_counter() - started) * 1000, 1)}
        if self._observer is not None:
            try:
                self._observer(ctx, name, tool_id, args, payload, timing)
            except Exception:
                log.exception("[anvil_tool_source] observer failed")
        # A JSON string reads the same on every backend (some str() dicts).
        return json.dumps(payload)
