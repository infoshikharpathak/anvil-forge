"""Platform: the public API.

    platform = Platform.from_config("bastion.yaml", app="ironclad")
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
import os
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
from anvil.namespaces import app_of, relative, resolve, short_name
from anvil.policy import resolve_agent_policy

import httpx

from .agents import ClientProvider, PlatformFactory
from .classifier import ProviderClassifier, default_provider
from .fallback import fallback_reply, output_block
from .config import POLICY_NAME, CompiledConfig, ConfigError, load_config
from .remote import BundleClient, BundleResult, ServiceError
from .hooks import AnvilHooks
from .tool_source import AnvilToolSource
from .trace import (
    Collectors, DashboardSink, LocalSink, RunCollector, TraceExporter, TraceRecorderHooks, TraceSink, build_trace,
)

log = logging.getLogger(__name__)


class PlatformError(Exception):
    pass


@dataclass
class _Remote:
    client: BundleClient
    etag: str
    interval: float
    from_cache: bool = False


def _cached_or_fail(client: BundleClient, url: str, error: Exception) -> BundleResult:
    cached = client.load_cache()
    if cached is None:
        raise PlatformError(
            f"Can't reach the bastion service at {url} ({error}) and there's no cached bundle "
            f"at {client.cache_path}. Start the service, or run once while it's up to create the cache."
        ) from None
    return cached


def _library_dashboard(compiled: CompiledConfig, transport: Any = None) -> DashboardSink | None:
    """Library mode's DashboardSink: from the YAML's dashboard section, with
    url and key falling back to BASTION_URL and BASTION_KEY. Those env vars
    alone also turn it on (metadata only)."""
    d = compiled.platform.dashboard
    env_url, env_key = os.environ.get("BASTION_URL"), os.environ.get("BASTION_KEY")
    if d is None and not (env_url and env_key):
        return None
    url = (d.url if d else None) or env_url
    key = (d.key if d else None) or env_key
    if not url or not key:
        log.warning("[platform] the config has a dashboard section but no %s; traces stay local. "
                    "Set BASTION_URL and BASTION_KEY.", "url" if not url else "key")
        return None
    return DashboardSink(url, key, include_content=d.include_content if d else False,
                         flush_interval=d.flush_interval if d else 5.0, batch_size=d.batch_size if d else 20,
                         transport=transport)


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
    config_version: int = 0


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
        prepare_tool_call: Any = None,
        on_output_blocked: Any = None,
        bundle_revision: int | None = None,
    ) -> None:
        """*prepare_tool_call* ``(ctx, tool_name, params) -> params`` runs on
        every tool call before Anvil sees it. Use it for values the model
        must not choose, such as the signed-in user's ID or idempotency keys.
        ``tool_name`` is the app-relative name (``get_accounts``)."""
        if app not in compiled.platform.apps:
            raise PlatformError(f"App {app!r} is not declared in the config.")
        self.app = app
        self._clients = ClientProvider(openai=openai_client, anthropic=anthropic_client)
        self._collectors = Collectors()
        self._http_transport = http_transport  # test seam for Anvil's upstream calls
        self._prepare = prepare_tool_call
        # async (agent, tool_calls, reason) -> [{"tool", "id", "what", "where", "flagged": bool}]:
        # flag what a turn created when its reply is blocked, or delivered but
        # labeled unrequested_action (see _run_agent).
        self._on_output_blocked = on_output_blocked
        self._output_checks: dict[str, dict[str, Any]] = {}  # run_id -> what the output check decided
        self._run_anvils: dict[str, Anvil] = {}
        self._sessions: dict[str, _Session] = {}
        self._exporter = TraceExporter(sinks if sinks is not None else [LocalSink()])
        self.dashboard: DashboardSink | None = next(
            (s for s in self._exporter.sinks if isinstance(s, DashboardSink)), None)
        self._content_override: bool | None = None  # from_service(include_content=...) beats the bundle
        self._started = False
        self._lock = asyncio.Lock()
        self._remote: _Remote | None = None
        self._refresh_task: asyncio.Task | None = None
        # Bumped on every config swap; sessions compare it to know they're stale.
        self._config_version = 0
        self._install(compiled, bundle_revision)

    def _install(self, compiled: CompiledConfig, bundle_revision: int | None) -> None:
        """Set the config and everything derived from it. Runs already in
        progress keep what they captured at their start."""
        self.config = compiled
        self.bundle_revision = bundle_revision
        self._session_ttl = compiled.anvil.settings.session_ttl
        cdef = compiled.platform.classifier
        self._classifier = None
        if cdef.enabled:
            backends = [a.backend for a in compiled.agents_of(self.app).values()]
            self._classifier = ProviderClassifier(
                self._clients, cdef.provider or default_provider(backends), cdef.model,
                on_usage=self._collectors.on_scan,
            )
        self._template_anvil = self._new_anvil(session_id=f"{self.app}-template")
        self._source = AnvilToolSource(
            self._template_anvil, self.app,
            observer=self._collectors.on_tool_call,
            anvil_for=lambda ctx: self._run_anvils.get(ctx.run_id),
            prepare=self._prepare,
        )

    @classmethod
    def from_config(
        cls, source: str | Path | dict, app: str, *, dashboard: bool = True,
        dashboard_transport: Any = None, **kwargs: Any,
    ) -> Platform:
        """Library mode. Traces go to a LocalSink, plus a DashboardSink when
        the config has a ``dashboard`` section or BASTION_URL and BASTION_KEY
        are set (``dashboard=False`` turns that off). Passing ``sinks=``
        replaces both."""
        compiled = load_config(source)
        if "sinks" not in kwargs and dashboard:
            sink = _library_dashboard(compiled, dashboard_transport)
            if sink is not None:
                kwargs["sinks"] = [LocalSink(), sink]
        return cls(compiled, app, **kwargs)

    @classmethod
    def from_service(
        cls,
        url: str,
        api_key: str,
        *,
        cache_dir: str | Path | None = None,
        refresh_interval: float = 30.0,
        service_transport: Any = None,
        dashboard: bool = True,
        include_content: bool | None = None,
        dashboard_transport: Any = None,
        **kwargs: Any,
    ) -> Platform:
        """Run on the bundle the bastion service publishes for the key's app.

        Fetches the bundle now and caches it on disk. If the service can't be
        reached, it runs on the cached bundle; with no cache it raises
        PlatformError. ``start()`` begins polling every *refresh_interval*
        seconds (ETag, so unchanged bundles cost a 304); a new revision is
        swapped in without a restart. *service_transport* is an httpx
        transport for reaching the service (tests).

        Traces go to a LocalSink and, unless ``dashboard=False``, a
        DashboardSink posting to the same service with the same key. Content
        is sent if the bundle's ``dashboard.include_content`` says so, unless
        *include_content* is given here. Passing ``sinks=`` replaces both.
        """
        client = BundleClient(url, api_key, cache_dir=cache_dir, transport=service_transport)
        try:
            result = client.fetch()
        except ServiceError as e:
            if e.status in (401, 403):
                raise PlatformError(f"The bastion service at {url} rejected the app key ({e}).") from None
            result = _cached_or_fail(client, url, e)
        except httpx.HTTPError as e:
            result = _cached_or_fail(client, url, e)
        else:
            client.save_cache(result)
        if result.from_cache:
            log.warning("[platform] bastion service unreachable; running on cached revision %s of %r",
                        result.revision, result.app)
        try:
            compiled = load_config(result.bundle)
        except ConfigError as e:
            raise PlatformError(f"The bundle for {result.app!r} (revision {result.revision}) doesn't compile: {e}") from None
        if "sinks" not in kwargs and dashboard:
            d = compiled.platform.dashboard
            content = include_content if include_content is not None else bool(d and d.include_content)
            kwargs["sinks"] = [LocalSink(), DashboardSink(url, api_key, include_content=content,
                                                          transport=dashboard_transport)]
        platform = cls(compiled, result.app, bundle_revision=result.revision, **kwargs)
        platform._content_override = include_content
        platform._remote = _Remote(client, result.etag, refresh_interval, from_cache=result.from_cache)
        return platform

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Register this app's tools with Agent Forge (and, in remote mode,
        start polling the service). Runs call it lazily."""
        async with self._lock:
            if not self._started:
                await register_tool_source(self._source)
                self._started = True
                if self._remote and self._remote.interval > 0 and self._refresh_task is None:
                    self._refresh_task = asyncio.create_task(self._refresh_loop())

    async def close(self) -> None:
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except (asyncio.CancelledError, Exception):
                pass
            self._refresh_task = None
        async with self._lock:
            if self._started:
                unregister_tool_source(self._source)
                self._started = False
        await self._exporter.close()  # DashboardSink sends what it still holds

    # ── remote mode ──────────────────────────────────────────────────────────

    async def refresh(self) -> bool:
        """Ask the service for a newer bundle now. Returns True if a new
        revision was applied. Never raises: a failure keeps the current config."""
        remote = self._remote
        if remote is None:
            return False
        try:
            result = await asyncio.to_thread(remote.client.fetch, remote.etag)
        except Exception as e:
            log.warning("[platform] bundle refresh failed, keeping revision %s: %s", self.bundle_revision, e)
            return False
        if result is None:
            return False  # 304: still current
        return await self._apply_bundle(result)

    async def _refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(self._remote.interval)
            await self.refresh()

    async def _apply_bundle(self, result: BundleResult) -> bool:
        if result.app != self.app:
            log.error("[platform] service sent a bundle for %r, expected %r; ignored", result.app, self.app)
            return False
        try:
            compiled = load_config(result.bundle)
        except ConfigError as e:
            log.error("[platform] revision %s doesn't compile, keeping %s: %s", result.revision, self.bundle_revision, e)
            return False
        async with self._lock:
            old = (self.config, self.bundle_revision, self._classifier, self._template_anvil, self._source)
            started = self._started
            if started:
                unregister_tool_source(self._source)
            self._install(compiled, result.revision)
            if started:
                try:
                    await register_tool_source(self._source)
                except Exception as e:  # e.g. a new tool name collides: keep running the old one
                    log.error("[platform] couldn't register revision %s's tools, keeping %s: %s",
                              result.revision, old[1], e)
                    (self.config, self.bundle_revision, self._classifier, self._template_anvil, self._source) = old
                    await register_tool_source(self._source)
                    return False
            self._config_version += 1
            if self.dashboard is not None and self._content_override is None:
                d = compiled.platform.dashboard
                self.dashboard.include_content = bool(d and d.include_content)
            self._remote.etag = result.etag
            self._remote.from_cache = False
        await asyncio.to_thread(self._remote.client.save_cache, result)
        log.info("[platform] now on revision %s of %r", result.revision, self.app)
        return True

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
            sess = _Session(session_id, self._new_anvil(session_id=f"session:{session_id}"),
                            config_version=self._config_version)
            self._sessions[session_id] = sess
        sess.last_used = now
        return sess

    async def _catch_up(self, sess: _Session) -> None:
        """Move a session onto the current config (called under its lock).

        It keeps Anvil's sequence state (records are copied into a new Anvil
        built from the new policy) and each agent's conversation history (the
        agent is rebuilt with the new prompt and given the old messages). So
        an edited prompt applies from the session's next turn, mid-chat.
        """
        if sess.config_version == self._config_version:
            return
        old_anvil = sess.anvil
        new_anvil = self._new_anvil(session_id=old_anvil.session_id)
        for agent_id in {**old_anvil.policy_engine.policy.agents, **self.config.platform.agents}:
            for call in old_anvil.call_log.calls_for(old_anvil.session_id, agent_id):
                new_anvil.call_log.record(new_anvil.session_id, call)
        factory = PlatformFactory(self.config, self.app, self._clients, route=self._source.call)
        agents = {}
        for agent_id, old_agent in sess.agents.items():
            if agent_id not in self.config.platform.agents:
                continue  # removed from the app: it starts fresh if it ever returns
            new_agent = await factory.create(name=agent_id)
            # Same backend classes keep history as a message list.
            if hasattr(old_agent, "_messages") and hasattr(new_agent, "_messages"):
                new_agent._messages = list(old_agent._messages)
            agents[agent_id] = new_agent
        sess.anvil, sess.agents, sess.config_version = new_anvil, agents, self._config_version

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

    def session_records(self, session_id: str) -> list[dict[str, Any]]:
        """Every Anvil record in a session, oldest first: tool calls and
        agent checks (``kind``), with params, result, violation, correction
        and latency. Unknown or expired sessions return []."""
        sess = self._sessions.get(session_id)
        if sess is None:
            return []
        records = []
        for agent_id in self.config.agents_of(self.app):
            for call in sess.anvil.call_log.calls_for(sess.anvil.session_id, agent_id):
                records.append(call.model_dump(mode="json"))
        records.sort(key=lambda r: r["timestamp"])
        return records

    def session_prerequisites(self, session_id: str, agent: str) -> list[str]:
        """Tools *agent* has already run successfully in this session, i.e.
        the prerequisites it has satisfied for its sequence rules. Names are
        app-relative."""
        agent_id = self._qualify(agent, self.config.platform.agents, "agent")
        sess = self._sessions.get(session_id)
        if sess is None:
            return []
        done = sess.anvil.call_log.history_names(sess.anvil.session_id, agent_id)
        return [relative(t, self.app) for t in done]

    def describe_agent(self, agent: str) -> dict[str, Any]:
        """An agent's effective policy, as Anvil enforces it: its tools with
        resolved trust, sequence rules, strictness, and the governance
        settings. Names are app-relative."""
        agent_id = self._qualify(agent, self.config.platform.agents, "agent")
        spec = self.config.platform.agents[agent_id]
        policy = resolve_agent_policy(agent_id, self.config.anvil.policies[POLICY_NAME].agents[agent_id])
        tools = []
        for tool_id in policy.allowed_tools:
            tool = self.config.anvil.tools[tool_id]
            tools.append({
                "name": relative(tool_id, self.app),
                # Anvil's rule: the agent's override, else the tool's own
                # trust, else the settings default.
                "trust": policy.trust_overrides.get(tool_id) or tool.trust or self.config.anvil.settings.default_trust,
                "injection_scanning": tool.injection_scanning,
                "description": tool.description,
            })
        settings = self.config.anvil.settings
        return {
            "id": agent_id,
            "name": short_name(agent_id),
            "description": spec.description,
            "prompt_version": spec.prompt_version,
            "strictness": policy.strictness,
            "tools": tools,
            "sequences": [
                {
                    "before": relative(s.before, self.app),
                    "requires": [relative(r, self.app) for r in s.requires],
                    "match_fields": list(s.match_fields),
                }
                for s in policy.sequences
            ],
            "settings": {
                "injection_scanning": settings.injection_scanning,
                "default_trust": settings.default_trust,
                "session_ttl": settings.session_ttl,
            },
        }

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
            await self._catch_up(sess)
            result = await self._run_agent(agent_id, message, trace_id, sess)
            sess.last_used = time.monotonic()
            return result

    async def _run_agent(self, agent_id: str, message: str, trace_id: str | None, sess: _Session | None) -> RunResult:
        run = await self._begin("agent", agent_id, message, trace_id, sess)
        factory, tracer, collector, run_config = run
        content, status, outcome, error = "", "failed", "failed", None
        extra_trace: dict[str, Any] = {}
        try:
            agent = sess.agents.get(agent_id) if sess else None
            if agent is None:
                agent = await factory.create(name=agent_id)
                if sess is not None:
                    sess.agents[agent_id] = agent  # keeps its conversation history
            result = await execute_agent(agent, message, agent_id, tracer, self._hooks(tracer.run_id))
            content, status = result.content, result.status
            outcome = "success" if status == "success" else status
            blocked = output_block(tracer.hook_denials) if status == "withheld" else None
            check = self._output_checks.pop(tracer.run_id, None)
            if blocked is None and check is not None and check["result"] == "flag":
                # Delivered, but the reply pushes an action the user didn't ask
                # for (unrequested_action): flag what the turn created, so the
                # user is warned before approving it. Nothing is cancelled.
                calls = list(collector.tool_calls)
                created = await self._flag_blocked_turn(agent_id, calls, check["reason"] or check["label"])
                extra_trace = {"output_check": check, "flagged": [c["id"] for c in created if c.get("flagged")]}
                if any(not c.get("flagged") for c in created):
                    extra_trace["flag_failed"] = [c["id"] for c in created if not c.get("flagged")]
            if blocked is not None:
                # The output scan blocked the agent's reply: show none of the
                # model's text, but tell the user what this turn's tools did.
                # Anything the turn created stays pending (the reply may have
                # been a false positive): the app flags it for the user instead.
                calls = list(collector.tool_calls)
                created = await self._flag_blocked_turn(agent_id, calls, blocked["message"])
                content, kind = fallback_reply(calls, created)
                extra_trace = {
                    "output_blocked": {"reason": blocked["message"], "fallback_used": kind},
                    "output_check": check or {"check": None, "label": None, "result": "block",
                                              "reason": blocked["message"]},
                    "flagged": [c["id"] for c in created if c.get("flagged")],
                }
                if any(not c.get("flagged") for c in created):
                    extra_trace["flag_failed"] = [c["id"] for c in created if not c.get("flagged")]
            return_value = RunResult(content, status, agent_id, tracer.trace_id, tracer.run_id,
                                     session_id=sess.id if sess else None)
        except Exception as exc:
            error = repr(exc)
            raise
        finally:
            if sess is None:
                await factory.kill_all()
            trace = await self._finish(run, outcome=outcome, error=error, output=content,
                                       framework=run_config.platform.agents[agent_id].backend, extra=extra_trace)
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
            await self._catch_up(sess)
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
        factory, tracer, collector, _ = run
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
        return [TraceRecorderHooks(self._collectors),
                AnvilHooks(self._run_anvils[run_id], self.app, on_output_check=self._output_checks.__setitem__)]

    async def _begin(self, kind: str, target: str, message: str, trace_id: str | None, sess: _Session | None):
        await self.start()
        tracer = RunTracer(task=message, routing_tier="platform", app_id=self.app, trace_id=trace_id)
        # A session's runs share its Anvil instance; otherwise one per run.
        self._run_anvils[tracer.run_id] = sess.anvil if sess else self._new_anvil(session_id=tracer.run_id)
        collector = RunCollector(
            trace_id=tracer.trace_id, run_id=tracer.run_id, app=self.app,
            kind=kind, target=target, message=message, session_id=sess.id if sess else None,
            bundle_revision=self.bundle_revision,
        )
        self._collectors.start(collector)
        factory = PlatformFactory(self.config, self.app, self._clients, route=self._source.call)
        # The config this run started on: a refresh mid-run mustn't change how
        # the run executes, or how its trace describes it.
        return factory, tracer, collector, self.config

    async def _flag_blocked_turn(self, agent_id: str, calls: list[dict[str, Any]],
                                 reason: str) -> list[dict[str, Any]]:
        """Ask the app to flag what this blocked turn created. Never raises:
        a failing hook means nothing was reported as flagged."""
        if self._on_output_blocked is None:
            return []
        try:
            return list(await self._on_output_blocked(agent_id, calls, reason) or [])
        except Exception:
            log.exception("[platform] on_output_blocked failed")
            return []

    async def _finish(self, run, *, outcome: str, error: str | None, output: str, framework: str,
                      extra: dict[str, Any] | None = None) -> dict:
        """Persist the Agent Forge row, merge the trace and export it. Never raises."""
        _, tracer, collector, config = run
        anvil = self._run_anvils.pop(tracer.run_id, None)
        self._output_checks.pop(tracer.run_id, None)  # e.g. a flow agent's, or a failed run's
        self._collectors.end(tracer.run_id)
        trace: dict[str, Any] = {}
        try:
            await tracer.finish(outcome=outcome, error=error, framework_used=framework, report=output)
            row = await asyncio.to_thread(db.get_run, tracer.run_id)
            trace = build_trace(collector, tracer, row, anvil, config,
                                outcome=outcome, error=error, output=output)
            trace.update(extra or {})
            await self._exporter.export(trace)
        except Exception:
            log.exception("[platform] tracing failed for trace %s", tracer.trace_id)
        return trace


__all__ = ["FlowResult", "Platform", "PlatformError", "RunContext", "RunResult"]
