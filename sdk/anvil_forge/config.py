"""Platform config: one YAML/dict describing apps, tools, agents, flows and grants.

    apps:                        # Anvil Phase 2 format, tools live under their app
      ironclad:
        base_url: http://localhost:8001
        auth: {header: Authorization, secret_env: IRONCLAD_TOKEN, prefix: "Bearer "}
        tools:
          get_accounts: {path: /accounts, method: GET, trust: trusted}
    agents:                      # always qualified "app.agent"
      ironclad.support_agent:
        instructions: You help customers with their accounts.
        prompt_version: v3
        backend: openai          # openai | anthropic
        model: gpt-5.4-mini
        tools: [get_accounts, notify.send_email]   # bare = own app
        sequences: [{before: initiate_transfer, requires: [get_accounts]}]
    flows:                       # qualified "app.flow"; a graph of registered agents
      ironclad.dispute_flow:
        entry: triage
        nodes: [{agent: triage, task: Classify the dispute.}, {agent: resolver}]
        edges: [{from: triage, to: resolver}]
    grants:
      - {tool: notify.send_email, to_apps: [ironclad]}

Everything compiles into one Anvil config. Agent tool lists become each
agent's ``allowed_tools`` and flow edges become ``allowed_delegations``,
so the Anvil policy is the single source of truth for what an agent may
call: the runtime reads an agent's tools back from that policy (see
``CompiledConfig.agent_tools``), never from this file's lists.

Validation runs at load time. Unknown tools, cross-app tools or
delegations without a grant, unknown agents in flows, and so on raise
``ConfigError``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from anvil.config import ConfigError as AnvilConfigError
from anvil.config import load_config_dict
from anvil.models import AgentPolicy, AnvilConfig, SequenceRule
from anvil.namespaces import app_of, relative, resolve, short_name
from anvil.policy import resolve_agent_policy
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

POLICY_NAME = "anvil-forge"
Backend = Literal["openai", "anthropic"]


class ConfigError(Exception):
    """The platform config is invalid. Nothing runs."""


class AgentDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instructions: str
    prompt_version: str
    backend: Backend = "openai"
    model: str | None = None
    role: str = ""
    tools: list[str] = Field(default_factory=list)
    sequences: list[SequenceRule] = Field(default_factory=list)
    strictness: Literal["strict", "loose"] = "strict"


class FlowNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent: str
    task: str = ""


class FlowEdge(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_agent: str = Field(alias="from")
    to_agent: str = Field(alias="to")
    condition: str | None = None
    condition_key: str | None = None


class FlowDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entry: str
    nodes: list[FlowNode]
    edges: list[FlowEdge] = Field(default_factory=list)


class ClassifierDef(BaseModel):
    """LLM injection classifier handed to Anvil. Uses the agents' provider
    and keys; ``provider`` defaults to the bound app's most common backend."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    provider: Backend | None = None
    model: str | None = None


class PlatformConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    apps: dict[str, dict[str, Any]] = Field(default_factory=dict)
    agents: dict[str, AgentDef] = Field(default_factory=dict)
    flows: dict[str, FlowDef] = Field(default_factory=dict)
    grants: list[dict[str, Any]] = Field(default_factory=list)
    settings: dict[str, Any] = Field(default_factory=dict)
    """Passed through to Anvil's settings (injection_scanning, strictness, ...)."""
    classifier: ClassifierDef = Field(default_factory=ClassifierDef)

    @model_validator(mode="after")
    def _qualified_ids(self) -> PlatformConfig:
        for kind, keys in (("agent", self.agents), ("flow", self.flows)):
            for key in keys:
                if "." not in key:
                    raise ValueError(f"{kind} {key!r} must be qualified as 'app.{kind}'")
        return self


class CompiledConfig:
    """The validated platform config plus the Anvil config compiled from it."""

    def __init__(self, platform: PlatformConfig, anvil: AnvilConfig) -> None:
        self.platform = platform
        self.anvil = anvil

    def agent_tools(self, agent_id: str) -> list[str]:
        """Tool names the agent may use, read from the Anvil policy and spelled
        the way the agent calls them (bare for its own app, ``app__tool``)."""
        policy = self.anvil.policies[POLICY_NAME].agents[agent_id]
        resolved = resolve_agent_policy(agent_id, policy)
        return [relative(t, app_of(agent_id)) for t in resolved.allowed_tools]

    def agents_of(self, app: str) -> dict[str, AgentDef]:
        return {aid: a for aid, a in self.platform.agents.items() if app_of(aid) == app}

    def flows_of(self, app: str) -> dict[str, FlowDef]:
        return {fid: f for fid, f in self.platform.flows.items() if app_of(fid) == app}

    def flow_agent(self, flow_id: str, ref: str) -> str:
        """Qualified agent ID for a node/edge reference inside *flow_id*."""
        return resolve(ref, app_of(flow_id))


def load_config(source: str | Path | dict[str, Any]) -> CompiledConfig:
    """Load and validate a platform config from a YAML path or a dict."""
    if isinstance(source, dict):
        raw = source
    else:
        raw = yaml.safe_load(Path(source).read_text()) or {}
    try:
        platform = PlatformConfig.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(f"Invalid anvil-forge config:\n{e}") from e
    return compile_config(platform)


def compile_config(platform: PlatformConfig) -> CompiledConfig:
    errors: list[str] = []
    declared_apps = set(platform.apps)

    for aid in platform.agents:
        if app_of(aid) not in declared_apps:
            errors.append(f"agent {aid!r} belongs to undeclared app {app_of(aid)!r}")

    # Flow edges become delegations: an edge a -> b lets a hand off to b.
    delegations: dict[str, set[str]] = {aid: set() for aid in platform.agents}
    for fid, flow in platform.flows.items():
        node_ids = [resolve(n.agent, app_of(fid)) for n in flow.nodes]
        if len(set(node_ids)) != len(node_ids):
            errors.append(f"flow {fid!r} uses an agent in more than one node")
        for nid in node_ids:
            if nid not in platform.agents:
                errors.append(f"flow {fid!r} references unregistered agent {nid!r}")
            elif app_of(nid) != app_of(fid):
                # A flow runs under its app's caller binding, so another app's
                # agent could never run in it.
                errors.append(
                    f"flow {fid!r} node {nid!r} belongs to app {app_of(nid)!r}; "
                    f"flow nodes must be agents of app {app_of(fid)!r}"
                )
        if resolve(flow.entry, app_of(fid)) not in node_ids:
            errors.append(f"flow {fid!r} entry {flow.entry!r} is not one of its nodes")
        for edge in flow.edges:
            src, dst = resolve(edge.from_agent, app_of(fid)), resolve(edge.to_agent, app_of(fid))
            if src not in node_ids or dst not in node_ids:
                errors.append(f"flow {fid!r} edge {src} -> {dst} references a node not in the flow")
                continue
            delegations[src].add(dst)

    if errors:
        raise ConfigError("Invalid anvil-forge config:\n" + "\n".join(f"  - {e}" for e in errors))

    agents_policy = {
        aid: AgentPolicy(
            allowed_tools=list(a.tools),
            allowed_delegations=sorted(relative(d, app_of(aid)) for d in delegations[aid]),
            sequences=list(a.sequences),
            strictness=a.strictness,
        ).model_dump()
        for aid, a in platform.agents.items()
    }
    raw_anvil = {
        "apps": platform.apps,
        "policies": {POLICY_NAME: {"description": "Compiled by anvil-forge", "agents": agents_policy}},
        "grants": platform.grants,
        "settings": platform.settings,
    }
    try:
        anvil_config = load_config_dict(raw_anvil)
    except AnvilConfigError as e:
        raise ConfigError(str(e).replace("Invalid anvil config", "Invalid anvil-forge config")) from e
    return CompiledConfig(platform, anvil_config)


__all__ = [
    "AgentDef", "ClassifierDef", "CompiledConfig", "ConfigError", "FlowDef",
    "FlowEdge", "FlowNode", "POLICY_NAME", "PlatformConfig", "compile_config",
    "load_config", "short_name",
]
