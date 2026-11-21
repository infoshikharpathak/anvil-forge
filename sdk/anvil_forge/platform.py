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
import time
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
    session_id: str | None = None

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
    session_id: str | None = None

    def __str__(self) -> str:
        return self.content


@dataclass
class _Session:
    """One conversation: an Anvil session plus the agents that remember it."""

    id: str
    anvil: Anvil
    agents: dict[str, Any] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_used: float = field(default_factory=time.monotonic)


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
        self._sessions: dict[str, _Session] = {}
        self._session_ttl = compiled.anvil.settings.session_ttl
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

    # ── sessions ─────────────────────────────────────────────────────────────

    def _session(self, session_id: str) -> _Session:
        """The live session for *session_id*, created on first use. Sessions
        idle longer than Anvil's session_ttl are dropped first."""
        now = time.monotonic()
        for sid in [s for s, sess in self._sessions.items()
                    if now - sess.last_used > self._session_ttl and not sess.lock.locked()]:
            self._sessions.pop(sid, None)
        sess = self._sessions.get(session_id)
        if sess is None:
            sess = _Session(session_id, self._new_anvil(session_id=f"session:{session_id}"))
            self._sessions[session_id] = sess
        sess.last_used = now
        return sess

    async def end_session(self, session_id: str) -> bool:
        """Forget a session: its Anvil sequence state and agent memory.
        Returns False if there was no such session."""
        sess = self._sessions.pop(session_id, None)
        if sess is None:
            return False
        async with sess.lock:
            for agent in sess.agents.values():
                await agent.close()
        return True

    def list_sessions(self) -> list[str]:
        return list(self._sessions)

    # ── running ──────────────────────────────────────────────────────────────

    async def run_agent(
        self, name: str, message: str, *, trace_id: str | None = None, session_id: str | None = None
    ) -> RunResult:
        """Run one registered agent on *message*.

        With *session_id*, runs share one Anvil session (sequence rules span
        turns, e.g. verify-then-transfer across messages) and the same agent
        instance (it remembers the conversation). Runs in one session are
        serialized. Without it, each run starts clean.
        """
        agent_id = self._qualify(name, self.config.platform.agents, "agent")
        if session_id is None:
            return await self._run_agent(agent_id, message, trace_id, None)
        sess = self._session(session_id)
        async with sess.lock:
            result = await self._run_agent(agent_id, message, trace_id, sess)
            sess.last_used = time.monotonic()
            return result

    async def _run_agent(self, agent_id: str, message: str, trace_id: str | None, sess: _Session | None) -> RunResult:
        run = await self._begin("agent", agent_id, message, trace_id, sess)
        factory, tracer, collector = run
        content, status, outcome, error = "", "failed", "failed", None
        try:
            agent = sess.agents.get(agent_id) if sess else None
            if agent is None:
                agent = await factory.create(name=agent_id)
                if sess is not None:
                    sess.agents[agent_id] = agent  # keeps its conversation history
            result = await execute_agent(agent, message, agent_id, tracer, self._hooks(tracer.run_id))
            content, status = result.content, result.status
            outcome = "success" if status == "success" else status
            return_value = RunResult(content, status, agent_id, tracer.trace_id, tracer.run_id,
                                     session_id=sess.id if sess else None)
        except Exception as exc:
            error = repr(exc)
            raise
        finally:
            if sess is None:
                await factory.kill_all()
            trace = await self._finish(run, outcome=outcome, error=error, output=content,
                                       framework=self.config.platform.agents[agent_id].backend)
        return_value.trace = trace
        return return_value

    async def run_flow(
        self, name: str, message: str, *, trace_id: str | None = None, session_id: str | None = None
    ) -> FlowResult:
        """Run a registered flow on *message*.

        With *session_id*, the flow shares the session's Anvil state with
        earlier turns (and with run_agent calls in the same session). Flow
        agents start fresh each turn: the flow passes context between its
        nodes itself.
        """
        flow_id = self._qualify(name, self.config.platform.flows, "flow")
        if session_id is None:
            return await self._run_flow(flow_id, message, trace_id, None)
        sess = self._session(session_id)
        async with sess.lock:
            result = await self._run_flow(flow_id, message, trace_id, sess)
            sess.last_used = time.monotonic()
            return result

    async def _run_flow(self, flow_id: str, message: str, trace_id: str | None, sess: _Session | None) -> FlowResult:
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
        run = await self._begin("flow", flow_id, message, trace_id, sess)
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
        return FlowResult(content, messages, stopped_by, flow_id, tracer.trace_id, tracer.run_id, trace,
                          session_id=sess.id if sess else None)

    # ── internals ────────────────────────────────────────────────────────────

    def _new_anvil(self, session_id: str) -> Anvil:
        anvil = Anvil(self.config.anvil, POLICY_NAME, app=self.app, session_id=session_id,
                      classifier=self._classifier)
        if self._http_transport is not None:
            anvil._transport = self._http_transport
        return anvil

    def _hooks(self, run_id: str) -> list:
        return [TraceRecorderHooks(self._collectors), AnvilHooks(self._run_anvils[run_id], self.app)]

    async def _begin(self, kind: str, target: str, message: str, trace_id: str | None, sess: _Session | None):
        await self.start()
        tracer = RunTracer(task=message, routing_tier="platform", app_id=self.app, trace_id=trace_id)
        # A session's runs share its Anvil instance; otherwise one per run.
        self._run_anvils[tracer.run_id] = sess.anvil if sess else self._new_anvil(session_id=tracer.run_id)
        collector = RunCollector(
            trace_id=tracer.trace_id, run_id=tracer.run_id, app=self.app,
            kind=kind, target=target, message=message, session_id=sess.id if sess else None,
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
