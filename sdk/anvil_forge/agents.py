"""Building Agent Forge agents for registered platform agents.

Identity rule: a registered agent always runs under its qualified
``app.agent`` ID. That is its Agent Forge name, so ``RunContext.agent_name``
(what Anvil sees) is always the qualified ID.

Agents the platform did not register, for example ones Agent Forge's
dynamic planner invents, are not supported in platform mode. If one runs
anyway, ``anvil_agent_id`` maps its name to an ID that can never be in the
policy, so Anvil's ``unknown_agent`` hard block stops it. That holds even
when its bare name matches a registered agent's short name: a bound Anvil
would otherwise resolve ``"triage"`` to ``"ironclad.triage"``.
"""

from __future__ import annotations

import uuid
from typing import Any

from agent_forge.config.settings import Settings
from agent_forge.core.agent import BaseAgent
from agent_forge.core.factory import AgentFactory
from anvil.namespaces import app_of

from .config import AgentDef, CompiledConfig

UNREGISTERED = "unregistered:"


def anvil_agent_id(name: str | None, app: str, registered: set[str] | dict) -> str:
    """The agent ID to hand Anvil for an Agent Forge agent name."""
    if name and name in registered:
        return name
    return f"{app}.{UNREGISTERED}{name or 'anonymous'}"


class ClientProvider:
    """LLM clients shared by agents and the classifier, so both use the same
    provider and keys. Pass clients in (tests, custom base URLs) or let them
    be created lazily from Agent Forge's provider settings."""

    def __init__(self, openai: Any = None, anthropic: Any = None) -> None:
        self._clients: dict[str, Any] = {"openai": openai, "anthropic": anthropic}

    def get(self, provider: str) -> Any:
        if self._clients.get(provider) is None:
            cfg = Settings.for_provider(provider)
            if provider == "openai":
                from openai import AsyncOpenAI

                kwargs: dict[str, Any] = {"api_key": cfg.api_key}
                if cfg.base_url:
                    kwargs["base_url"] = cfg.base_url
                self._clients[provider] = AsyncOpenAI(**kwargs)
            else:
                from anthropic import AsyncAnthropic

                self._clients[provider] = AsyncAnthropic(api_key=cfg.api_key)
        return self._clients[provider]

    @staticmethod
    def default_model(provider: str) -> str:
        from agent_forge.config import settings

        return settings._DEFAULTS[provider]


class PlatformFactory(AgentFactory):
    """Agent Forge factory that builds registered agents from the platform
    config, ignoring whatever prompt/tools a caller (or planner) suggests:
    the config and the Anvil policy decide."""

    def __init__(self, compiled: CompiledConfig, app: str, clients: ClientProvider) -> None:
        super().__init__()
        self._compiled = compiled
        self._app = app
        self._clients = clients

    def model_for(self, agent_id: str) -> str:
        spec = self._compiled.platform.agents[agent_id]
        return spec.model or ClientProvider.default_model(spec.backend)

    async def create(
        self,
        role: str = "",
        name: str = "agent",
        *,
        system_message: str | None = None,
        **kwargs: Any,
    ) -> BaseAgent:
        spec = self._compiled.platform.agents.get(name)
        if spec is not None and app_of(name) == self._app:
            agent = self._build(name, spec)
        else:
            agent = self._build_unregistered(name, role, system_message)
        self._agents[agent.agent_id] = agent
        return agent

    def _build(self, agent_id: str, spec: AgentDef) -> BaseAgent:
        from agent_forge.tools import get_tools, to_openai_schema

        # The model is offered only the tools the Anvil policy allows this
        # agent. Every tool visible to the app has a handler, though, so a
        # call outside the policy (hallucinated, or injected) still goes to
        # Anvil, which returns its disallowed_tool correction to the model and
        # records the violation, instead of the backend answering "unknown tool".
        offered = get_tools(self._compiled.agent_tools(agent_id))
        schemas = [to_openai_schema(fn) for fn in offered]
        handlers = {
            getattr(fn, "_tool_name", fn.__name__): fn
            for fn in get_tools(self._compiled.visible_tools(self._app))
        }
        common = dict(
            agent_id=str(uuid.uuid4()),
            name=agent_id,
            role=spec.role or agent_id.split(".", 1)[1],
            client=self._clients.get(spec.backend),
            system_message=spec.instructions,
            model=self.model_for(agent_id),
            tool_handlers=handlers,
        )
        if spec.backend == "anthropic":
            from agent_forge.backends.anthropic import AnthropicAgent

            tools = [
                {
                    "name": s["function"]["name"],
                    "description": s["function"]["description"],
                    "input_schema": s["function"]["parameters"],
                }
                for s in schemas
            ]
            return AnthropicAgent(tools=tools, **common)
        from agent_forge.backends.openai import OpenAIAgent

        return OpenAIAgent(tools=schemas, **common)

    def _build_unregistered(self, name: str, role: str, system_message: str | None) -> BaseAgent:
        """An agent the platform doesn't know. It gets no tools, and Anvil
        blocks it as unknown_agent before it can run."""
        from agent_forge.backends.openai import OpenAIAgent

        return OpenAIAgent(
            agent_id=str(uuid.uuid4()),
            name=name,
            role=role or "unregistered",
            client=self._clients.get("openai"),
            system_message=system_message or "",
            model=ClientProvider.default_model("openai"),
        )
