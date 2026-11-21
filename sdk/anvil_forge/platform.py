"""Platform: the public API.

    platform = Platform.from_config("anvil-forge.yaml", app="ironclad")
    reply = await platform.run_agent("support_agent", message, trace_id=None)
    result = await platform.run_flow("dispute_flow", message)
    platform.list_agents(), platform.list_tools(), platform.list_flows()

The app passes bare names, and the Platform qualifies them with its bound
app. Each run:
- gets its own Anvil session, bound to the app (Phase 2 caller binding),
  so sequence rules are scoped to that run;
- runs through Agent Forge's shared execution path with AnvilHooks and the
  trace recorder;
- has its tool calls routed through the one registered AnvilToolSource;
- produces one combined trace, exported to the sinks (LocalSink by
  default) without ever failing the run.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_forge import db
from agent_forge.core.conversation import ConversationMessage, StopSignal
from agent_forge.core.context import RunContext
from agent_forge.core.execution import execute_agent
from agent_forge.core.graph_runner import GraphRunner
from agent_forge.core.orchestrator import GraphEdge, GraphNode, GraphSpec
from agent_forge.tools.sources import register_tool_source, unregister_tool_source
from agent_forge.tracing import RunTracer
from anvil import Anvil
from anvil.namespaces import app_of, resolve, short_name

from .agents import ClientProvider, PlatformFactory
from .classifier import ProviderClassifier, default_provider
from .config import POLICY_NAME, CompiledConfig, load_config
from .hooks import AnvilHooks
from .tool_source import AnvilToolSource
from .trace import Collectors, LocalSink, RunCollector, TraceExporter, TraceRecorderHooks, TraceSink, build_trace

log = logging.getLogger(__name__)


class PlatformError(Exception):
    pass


@dataclass
class RunResult:
    content: str
    status: str  # "success" | "blocked" | "withheld"
    agent: str
    trace_id: str
    run_id: str
    trace: dict[str, Any] = field(repr=False, default_factory=dict)

    def __str__(self) -> str:
        return self.content


@dataclass
class FlowResult:
    content: str  # the last message of the flow
    messages: list[dict[str, str]]
    stopped_by: str | None
    flow: str
    trace_id: str
    run_id: str
    trace: dict[str, Any] = field(repr=False, default_factory=dict)

    def __str__(self) -> str:
        return self.content


class Platform:
    def __init__(
        self,
        compiled: CompiledConfig,
        app: str,
        *,
        openai_client: Any = None,
        anthropic_client: Any = None,
        sinks: list[TraceSink] | None = None,
        http_transport: Any = None,
    ) -> None:
        if app not in compiled.platform.apps:
            raise PlatformError(f"App {app!r} is not declared in the config.")
        self.app = app
        self.config = compiled
        self._clients = ClientProvider(openai=openai_client, anthropic=anthropic_client)
        self._collectors = Collectors()
        self._http_transport = http_transport  # test seam for Anvil's upstream calls

        cdef = compiled.platform.classifier
        self._classifier = None
        if cdef.enabled:
            backends = [a.backend for a in compiled.agents_of(app).values()]
            self._classifier = ProviderClassifier(
                self._clients, cdef.provider or default_provider(backends), cdef.model,
                on_usage=self._collectors.on_scan,
            )

        self._run_anvils: dict[str, Anvil] = {}
        self._template_anvil = self._new_anvil(session_id=f"{app}-template")
        self._source = AnvilToolSource(
            self._template_anvil, app,
            observer=self._collectors.on_tool_call,
            anvil_for=lambda ctx: self._run_anvils.get(ctx.run_id),
        )
        self._exporter = TraceExporter(sinks if sinks is not None else [LocalSink()])
        self._started = False
        self._lock = asyncio.Lock()

    @classmethod
    def from_config(cls, source: str | Path | dict, app: str, **kwargs: Any) -> Platform:
        return cls(load_config(source), app, **kwargs)

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Register this app's tools with Agent Forge. Runs call it lazily."""
        async with self._lock:
            if not self._started:
                await register_tool_source(self._source)
                self._started = True

    async def close(self) -> None:
        async with self._lock:
            if self._started:
                unregister_tool_source(self._source)
                self._started = False

    async def __aenter__(self) -> Platform:
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    # ── introspection ────────────────────────────────────────────────────────

    def list_agents(self) -> list[dict[str, Any]]:
        return [
            {
                "name": short_name(aid),
                "id": aid,
                "prompt_version": spec.prompt_version,
                "backend": spec.backend,
                "model": spec.model or ClientProvider.default_model(spec.backend),
                "tools": self.config.agent_tools(aid),  # from the Anvil policy
            }
            for aid, spec in self.config.agents_of(self.app).items()
        ]

    def list_tools(self) -> list[str]:
        return self.config.visible_tools(self.app)

    def list_flows(self) -> list[str]:
        return [short_name(fid) for fid in self.config.flows_of(self.app)]

    def _qualify(self, name: str, registry: dict, kind: str) -> str:
        qualified = resolve(name, self.app)
        if app_of(qualified) != self.app:
            raise PlatformError(f"{kind} {name!r} belongs to app {app_of(qualified)!r}, not {self.app!r}.")
        if qualified not in registry:
            raise PlatformError(f"Unknown {kind} {name!r} in app {self.app!r}.")
        return qualified

    # ── running ──────────────────────────────────────────────────────────────

    async def run_agent(self, name: str, message: str, *, trace_id: str | None = None) -> RunResult:
        agent_id = self._qualify(name, self.config.platform.agents, "agent")
        run = await self._begin("agent", agent_id, message, trace_id)
        factory, tracer, collector = run
        content, status, outcome, error = "", "failed", "failed", None
        try:
            agent = await factory.create(name=agent_id)
            result = await execute_agent(agent, message, agent_id, tracer, self._hooks(tracer.run_id))
            content, status = result.content, result.status
            outcome = "success" if status == "success" else status
            return_value = RunResult(content, status, agent_id, tracer.trace_id, tracer.run_id)
        except Exception as exc:
            error = repr(exc)
            raise
        finally:
            await factory.kill_all()
            trace = await self._finish(run, outcome=outcome, error=error, output=content,
                                       framework=self.config.platform.agents[agent_id].backend)
        return_value.trace = trace
        return return_value

    async def run_flow(self, name: str, message: str, *, trace_id: str | None = None) -> FlowResult:
        flow_id = self._qualify(name, self.config.platform.flows, "flow")
        flow = self.config.platform.flows[flow_id]
        agents = self.config.platform.agents
        qid = lambda ref: self.config.flow_agent(flow_id, ref)  # noqa: E731
        spec = GraphSpec(
            nodes=[
                GraphNode(
                    name=qid(n.agent),
                    role_description=agents[qid(n.agent)].role or short_name(qid(n.agent)),
                    system_prompt=agents[qid(n.agent)].instructions,
                    task_prompt=n.task,
                )
                for n in flow.nodes
            ],
            edges=[
                GraphEdge(
                    from_node=qid(e.from_agent), to_node=qid(e.to_agent),
                    condition=e.condition, condition_key=e.condition_key,
                )
                for e in flow.edges
            ],
            entry=qid(flow.entry),
        )
        run = await self._begin("flow", flow_id, message, trace_id)
        factory, tracer, collector = run
        messages: list[dict[str, str]] = []
        stopped_by, outcome, error = None, "failed", None
        try:
            runner = GraphRunner(factory, spec, tracer=tracer, hooks=self._hooks(tracer.run_id))
            async for item in runner.run_stream(message):
                if isinstance(item, ConversationMessage):
                    messages.append({"agent": item.agent, "content": item.content})
                elif isinstance(item, StopSignal):
                    stopped_by = item.stopped_by
            outcome = "success"
        except Exception as exc:
            error = repr(exc)
            raise
        finally:
            await factory.kill_all()
            content = messages[-1]["content"] if messages else ""
            trace = await self._finish(run, outcome=outcome, error=error, output=content, framework="graph")
        return FlowResult(content, messages, stopped_by, flow_id, tracer.trace_id, tracer.run_id, trace)

    # ── internals ────────────────────────────────────────────────────────────

    def _new_anvil(self, session_id: str) -> Anvil:
        anvil = Anvil(self.config.anvil, POLICY_NAME, app=self.app, session_id=session_id,
                      classifier=self._classifier)
        if self._http_transport is not None:
            anvil._transport = self._http_transport
        return anvil

    def _hooks(self, run_id: str) -> list:
        return [TraceRecorderHooks(self._collectors), AnvilHooks(self._run_anvils[run_id], self.app)]

    async def _begin(self, kind: str, target: str, message: str, trace_id: str | None):
        await self.start()
        tracer = RunTracer(task=message, routing_tier="platform", app_id=self.app, trace_id=trace_id)
        self._run_anvils[tracer.run_id] = self._new_anvil(session_id=tracer.run_id)
        collector = RunCollector(
            trace_id=tracer.trace_id, run_id=tracer.run_id, app=self.app,
            kind=kind, target=target, message=message,
        )
        self._collectors.start(collector)
        factory = PlatformFactory(self.config, self.app, self._clients, route=self._source.call)
        return factory, tracer, collector

    async def _finish(self, run, *, outcome: str, error: str | None, output: str, framework: str) -> dict:
        """Persist the Agent Forge row, merge the trace and export it. Never raises."""
        _, tracer, collector = run
        anvil = self._run_anvils.pop(tracer.run_id, None)
        self._collectors.end(tracer.run_id)
        trace: dict[str, Any] = {}
        try:
            await tracer.finish(outcome=outcome, error=error, framework_used=framework, report=output)
            row = await asyncio.to_thread(db.get_run, tracer.run_id)
            trace = build_trace(collector, tracer, row, anvil, self.config,
                                outcome=outcome, error=error, output=output)
            await self._exporter.export(trace)
        except Exception:
            log.exception("[platform] tracing failed for trace %s", tracer.trace_id)
        return trace


__all__ = ["FlowResult", "Platform", "PlatformError", "RunContext", "RunResult"]
