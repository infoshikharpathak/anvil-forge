"""Combined trace: one record per run, keyed by trace_id.

It merges three sources:
- Agent Forge's run row: agents, model, tokens, latency, status, hook
  denials. This is RunTracer, persisted to Agent Forge's own SQLite DB.
- Anvil's call records for this trace_id: tool calls, agent checks and
  violations.
- What the platform observed directly (RunCollector): each agent's real
  input and raw output, every tool call with its params and response, and
  the classifier's token usage.

Sinks receive the merged dict:
- LocalSink (default): full detail (prompts, inputs, outputs, tool params
  and responses) as JSONL in the app's working directory.
- DashboardSink: sends a metadata-only copy to the bastion service
  (POST /v1/traces) in batches, in the background. Content (prompts,
  inputs, outputs, tool params) is opt-in and redacted.

TraceExporter never lets a sink block or fail a run: each export has a
timeout and every error is logged and swallowed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from agent_forge.core.hooks import AgentHooks, HookDecision
from anvil.namespaces import app_of

if TYPE_CHECKING:
    from agent_forge.core.context import RunContext
    from agent_forge.tracing import RunTracer
    from anvil import Anvil

    from .config import CompiledConfig

log = logging.getLogger(__name__)


# ── collection during a run ───────────────────────────────────────────────────

@dataclass
class RunCollector:
    trace_id: str
    run_id: str
    app: str
    kind: str  # "agent" | "flow"
    target: str  # qualified agent or flow ID
    message: str
    session_id: str | None = None
    bundle_revision: int | None = None  # remote mode: which service revision this run used
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    _t0: float = field(default_factory=time.perf_counter, repr=False)
    inputs: dict[str, list[str]] = field(default_factory=dict)
    raw_outputs: dict[str, list[str]] = field(default_factory=dict)
    tool_calls: list[dict] = field(default_factory=list)
    scans: list[dict] = field(default_factory=list)
    agents_seen: list[str] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)

    def saw(self, agent: str) -> None:
        if agent not in self.agents_seen:
            self.agents_seen.append(agent)

    def offset_ms(self, at: float | None = None) -> float:
        """Milliseconds since the run began (perf_counter clock)."""
        return round(((at if at is not None else time.perf_counter()) - self._t0) * 1000, 1)

    def step(self, type: str, agent: str | None, at: float | None = None, **fields: Any) -> dict:
        entry = {"type": type, "agent": agent, "offset_ms": self.offset_ms(at), **fields}
        self.steps.append(entry)
        return entry


class Collectors:
    """Active collectors by run_id. Hooks, tool calls and scans find theirs
    through the RunContext, so concurrent runs never mix."""

    def __init__(self) -> None:
        self._by_run: dict[str, RunCollector] = {}

    def start(self, collector: RunCollector) -> None:
        self._by_run[collector.run_id] = collector

    def end(self, run_id: str) -> RunCollector | None:
        return self._by_run.pop(run_id, None)

    def for_ctx(self, ctx: RunContext | None) -> RunCollector | None:
        return self._by_run.get(ctx.run_id) if ctx is not None else None

    # observers wired into AnvilToolSource / ProviderClassifier
    def on_tool_call(self, ctx: RunContext, name: str, tool_id: str, args: dict, response: dict,
                     timing: dict | None = None) -> None:
        c = self.for_ctx(ctx)
        if c is None:
            return
        correction = response.get("correction") or {}
        violation = correction.get("violation")
        status = tool_status(violation, response.get("tool_executed"), response.get("success"))
        started = (timing or {}).get("started")
        duration = (timing or {}).get("duration_ms")
        c.tool_calls.append({
            "agent": ctx.agent_name,
            "tool": name,
            "tool_id": tool_id,
            "tool_app": app_of(tool_id),
            "params": args,
            "success": response.get("success"),
            "tool_executed": response.get("tool_executed"),
            "status": status,
            "violation": violation,
            "correction": correction.get("message"),
            "required_actions": correction.get("required_actions") or [],
            "response": response.get("data"),
            "started_ms": c.offset_ms(started) if started is not None else None,
            "duration_ms": duration,
        })
        c.step("tool_call", ctx.agent_name, at=started, tool=name)
        c.step("tool_result", ctx.agent_name, tool=name, status=status, violation=violation,
               duration_ms=duration)

    def on_scan(self, ctx: RunContext | None, usage: dict) -> None:
        c = self.for_ctx(ctx)
        if c is not None:
            c.scans.append({"agent": ctx.agent_name, **usage})


class TraceRecorderHooks(AgentHooks):
    """Records each agent's input and raw output. Always allows. Placed
    first in the hook list, so blocked inputs are recorded too."""

    def __init__(self, collectors: Collectors) -> None:
        self._collectors = collectors

    async def before_agent_run(self, ctx, task):
        c = self._collectors.for_ctx(ctx)
        if c is not None:
            c.saw(ctx.agent_name)
            c.inputs.setdefault(ctx.agent_name, []).append(task)
            c.step("agent_start", ctx.agent_name)
        return HookDecision.allow()

    async def after_agent_run(self, ctx, output):
        c = self._collectors.for_ctx(ctx)
        if c is not None:
            c.raw_outputs.setdefault(ctx.agent_name, []).append(output)
            c.step("agent_end", ctx.agent_name)
        return HookDecision.allow()

    async def on_handoff(self, ctx, from_agent, to_agent, payload):
        c = self._collectors.for_ctx(ctx)
        if c is not None:
            c.step("handoff", from_agent, to_agent=to_agent)
        return HookDecision.allow()


_POLICY_BLOCKS = {
    "unknown_agent", "disallowed_tool", "missing_prerequisite", "cross_app_denied",
    "injection_detected_request", "injection_detected_response", "schema_violation",
}


def tool_status(violation: str | None, executed: bool | None, success: bool | None) -> str:
    """allowed | blocked (Anvil refused it) | error (the call failed)."""
    if violation in _POLICY_BLOCKS:
        return "blocked"
    if violation or not success:
        return "error"
    return "allowed"


def _close_steps(steps: list[dict], agents: list[dict]) -> list[dict]:
    """Sorted steps, with an agent_end added for any agent whose run ended
    without after_agent_run (its input was blocked, or it raised). The end
    sits at the agent's start plus its Agent Forge latency."""
    steps = sorted(steps, key=lambda s: s["offset_ms"])
    status_by_agent: dict[str, list[dict]] = {}
    for a in agents:
        status_by_agent.setdefault(a["agent"], []).append(a)
    seen: dict[str, int] = {}
    out: list[dict] = []
    open_starts: dict[str, dict] = {}
    for st in steps:
        name = st.get("agent")
        if st["type"] == "agent_start":
            if name in open_starts:
                out.append(_synth_end(open_starts.pop(name), status_by_agent, seen))
            open_starts[name] = st
        elif st["type"] == "agent_end" and name in open_starts:
            start = open_starts.pop(name)
            i = seen.get(name, 0)
            seen[name] = i + 1
            runs = status_by_agent.get(name, [])
            st = {**st, "duration_ms": round(st["offset_ms"] - start["offset_ms"], 1),
                  "status": runs[i]["status"] if i < len(runs) else None}
        out.append(st)
    for start in open_starts.values():
        out.append(_synth_end(start, status_by_agent, seen))
    return sorted(out, key=lambda s: s["offset_ms"])


def _synth_end(start: dict, status_by_agent: dict, seen: dict) -> dict:
    name = start.get("agent")
    i = seen.get(name, 0)
    seen[name] = i + 1
    runs = status_by_agent.get(name, [])
    run = runs[i] if i < len(runs) else {}
    latency = run.get("latency_ms") or 0
    return {"type": "agent_end", "agent": name, "offset_ms": round(start["offset_ms"] + latency, 1),
            "duration_ms": latency, "status": run.get("status")}


# ── merging ───────────────────────────────────────────────────────────────────

def _anvil_records(anvil: Anvil, trace_id: str, agent_ids: list[str], since: str) -> list[dict]:
    """This run's records: matching trace_id, and made since the run began.
    A session's Anvil holds earlier turns too, possibly under the same
    caller trace_id."""
    records = []
    for agent_id in agent_ids:
        for call in anvil.call_log.calls_for(anvil.session_id, agent_id):
            if call.trace_id == trace_id and call.timestamp >= since:
                records.append(call.model_dump(mode="json"))
    records.sort(key=lambda r: r["timestamp"])
    return records


def build_trace(
    collector: RunCollector,
    tracer: RunTracer,
    forge_row: dict | None,
    anvil: Anvil,
    compiled: CompiledConfig,
    *,
    outcome: str,
    error: str | None = None,
    output: str | None = None,
) -> dict[str, Any]:
    from .agents import anvil_agent_id

    from agent_forge.tracing import _estimate_cost

    registered = compiled.platform.agents
    agent_ids = list(dict.fromkeys(
        list(registered) + [anvil_agent_id(a, collector.app, registered) for a in collector.agents_seen]
    ))
    anvil_records = _anvil_records(anvil, collector.trace_id, agent_ids, collector.started_at)

    per_agent_seen: dict[str, int] = {}
    agents = []
    for entry in tracer.agents_spawned:
        name = entry["agent_id"]
        i = per_agent_seen.get(name, 0)
        per_agent_seen[name] = i + 1
        spec = registered.get(name)
        inputs = collector.inputs.get(name, [])
        raws = collector.raw_outputs.get(name, [])
        agents.append({
            "agent": name,
            "registered": spec is not None,
            "prompt_version": spec.prompt_version if spec else None,
            "backend": spec.backend if spec else None,
            "model": entry["model"] or None,
            "status": entry["status"],
            "input_tokens": entry["input_tokens"],
            "output_tokens": entry["output_tokens"],
            "cost_usd": round(_estimate_cost(entry["model"], entry["input_tokens"], entry["output_tokens"]), 6),
            "latency_ms": entry["latency_ms"],
            "prompt": spec.instructions if spec else None,
            "input": inputs[i] if i < len(inputs) else None,
            "raw_output": raws[i] if i < len(raws) else None,
            "output": entry["content"],
        })

    violations = [
        {
            "agent": r["agent"],
            "kind": r["kind"],
            "tool": r["tool"] if r["kind"] == "tool" else None,
            "target_agent": r.get("target_agent"),
            "violation": r["violation"],
            "message": r["correction_message"],
        }
        for r in anvil_records if r.get("violation")
    ]

    tool_calls = []
    for call in collector.tool_calls:
        tool = compiled.anvil.tools.get(call["tool_id"])
        redact = list(getattr(tool, "redact_fields", None) or []) if tool is not None else []
        tool_calls.append({**call, "redacted_fields": redact if call.get("tool_executed") else []})

    flow_entry = None
    if collector.kind == "flow" and collector.target in compiled.platform.flows:
        flow_entry = compiled.flow_agent(collector.target, compiled.platform.flows[collector.target].entry)

    scan_in = sum(s["input_tokens"] for s in collector.scans)
    scan_out = sum(s["output_tokens"] for s in collector.scans)
    scan_cost = round(sum(s["cost_usd"] for s in collector.scans), 6)
    agent_cost = round(sum(a["cost_usd"] for a in agents), 6)

    return {
        "trace_id": collector.trace_id,
        "run_id": collector.run_id,
        "session_id": collector.session_id,
        "bundle_revision": collector.bundle_revision,
        "app": collector.app,
        "kind": collector.kind,
        "target": collector.target,
        "flow_entry": flow_entry,
        "started_at": collector.started_at,
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "outcome": outcome,
        "error": error,
        "input": collector.message,
        "output": output,
        "agents": agents,
        "tool_calls": tool_calls,
        "steps": _close_steps(collector.steps, agents),
        "violations": violations,
        "hook_denials": list(tracer.hook_denials),
        "scans": {
            "calls": len(collector.scans),
            "input_tokens": scan_in,
            "output_tokens": scan_out,
            "cost_usd": scan_cost,
            "detail": collector.scans,
        },
        "totals": {
            "input_tokens": sum(a["input_tokens"] for a in agents) + scan_in,
            "output_tokens": sum(a["output_tokens"] for a in agents) + scan_out,
            "cost_usd": round(agent_cost + scan_cost, 6),
            "agent_cost_usd": agent_cost,
            "scan_cost_usd": scan_cost,
            "latency_ms": round((time.perf_counter() - collector._t0) * 1000, 1),
        },
        "anvil_records": anvil_records,
        "forge_run": {
            k: forge_row.get(k)
            for k in ("run_id", "trace_id", "outcome", "guardrails_triggered",
                      "total_input_tokens", "total_output_tokens", "total_latency_ms")
        } if forge_row else None,
    }


# ── sinks ─────────────────────────────────────────────────────────────────────

class TraceSink(ABC):
    @abstractmethod
    async def export(self, trace: dict[str, Any]) -> None: ...


class LocalSink(TraceSink):
    """Full-detail traces as JSON lines in the app (default ./bastion_traces.jsonl)."""

    def __init__(self, path: str | Path = "bastion_traces.jsonl") -> None:
        self.path = Path(path)

    async def export(self, trace: dict[str, Any]) -> None:
        line = json.dumps(trace, default=str)
        await asyncio.to_thread(self._append, line)

    def _append(self, line: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(line + "\n")

    def read(self, trace_id: str | None = None) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        rows = [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]
        return [r for r in rows if trace_id is None or r["trace_id"] == trace_id]


# ── the dashboard copy ────────────────────────────────────────────────────────

REDACTED = "[redacted]"
# Keys whose values never leave the app, at any depth, even with content on.
SECRET_KEYS = re.compile(r"pass(word)?|secret|token|api[_-]?key|authorization|cookie|otp|pin|cvv|ssn", re.I)
_PATTERN = re.compile(r"\('([^']*)'\)")


def _redact(value: Any, extra: set[str]) -> Any:
    if isinstance(value, dict):
        return {k: REDACTED if (k in extra or SECRET_KEYS.search(str(k))) else _redact(v, extra)
                for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v, extra) for v in value]
    return value


def _status(trace: dict[str, Any]) -> str:
    outcome = trace.get("outcome")
    if outcome == "failed" or trace.get("error"):
        return "error"
    if outcome in ("blocked", "withheld") or trace.get("hook_denials"):
        return "blocked"
    return "success"


def dashboard_record(trace: dict[str, Any], *, include_content: bool = False) -> dict[str, Any]:
    """The copy of a full trace that goes to the bastion service.

    Metadata only: IDs, agents, prompt versions, models, tokens, cost,
    timings, tool names and statuses, violation types and scan verdicts.
    With *include_content*, prompts, inputs, outputs, tool params and tool
    responses go too, with secret-looking keys and each tool's
    redact_fields replaced by "[redacted]".
    """
    agents = trace.get("agents") or []
    is_flow = trace.get("kind") == "flow"
    agent = (trace.get("flow_entry") or (agents[0]["agent"] if agents else trace.get("target"))) if is_flow \
        else trace.get("target")
    first = next((a for a in agents if a["agent"] == agent), agents[0] if agents else {})
    models = [a["model"] for a in agents if a.get("model")]
    totals = trace.get("totals") or {}

    tool_calls = [{
        "tool": c["tool"], "tool_id": c["tool_id"], "app": c.get("tool_app") or app_of(c["tool_id"]),
        "agent": c.get("agent"), "status": c.get("status") or tool_status(c.get("violation"), c.get("tool_executed"), c.get("success")),
        "violation": c.get("violation"), "started_ms": c.get("started_ms"), "duration_ms": c.get("duration_ms"),
        "redacted_fields": c.get("redacted_fields") or [],
    } for c in trace.get("tool_calls") or []]

    scans = [{"source": "classifier", "agent": s.get("agent"), "model": s.get("model"),
              "detected": s.get("detected"), "pattern": s.get("pattern"),
              "input_tokens": s.get("input_tokens", 0), "output_tokens": s.get("output_tokens", 0)}
             for s in (trace.get("scans") or {}).get("detail") or []]
    for v in trace.get("violations") or []:
        if str(v.get("violation", "")).startswith("injection_detected"):
            m = _PATTERN.search(v.get("message") or "")
            scans.append({"source": "anvil", "agent": v.get("agent"), "detected": True,
                          "violation": v["violation"], "tool": v.get("tool"),
                          "pattern": m.group(1) if m else None})

    record: dict[str, Any] = {
        "trace_id": trace["trace_id"], "run_id": trace["run_id"], "app": trace["app"],
        "kind": trace.get("kind", "agent"), "agent": agent,
        "flow": trace.get("target") if is_flow else None,
        "session_id": trace.get("session_id"), "bundle_revision": trace.get("bundle_revision"),
        "prompt_version": first.get("prompt_version"),
        "started_at": trace["started_at"], "ended_at": trace["ended_at"],
        "duration_ms": totals.get("latency_ms"), "status": _status(trace),
        "model": max(set(models), key=models.count) if models else None,
        "token_input": totals.get("input_tokens", 0), "token_output": totals.get("output_tokens", 0),
        "cost_usd": totals.get("cost_usd", 0.0),
        "tool_calls": tool_calls,
        "steps": [{k: v for k, v in st.items()} for st in trace.get("steps") or []],
        "agents": [{k: a.get(k) for k in ("agent", "registered", "prompt_version", "backend", "model", "status",
                                          "input_tokens", "output_tokens", "cost_usd", "latency_ms")}
                   for a in agents],
        "violations": [{k: v.get(k) for k in ("agent", "kind", "tool", "target_agent", "violation")}
                       for v in trace.get("violations") or []],
        "scans": scans,
        "hook_denials": len(trace.get("hook_denials") or []),
        "content": None,
    }
    if include_content:
        redact_by_tool = {c["tool_id"]: set(c.get("redacted_fields") or []) for c in trace.get("tool_calls") or []}
        record["content"] = {
            "input": trace.get("input"),
            "output": trace.get("output"),
            "error": trace.get("error"),
            "agents": [{"agent": a["agent"], "prompt": a.get("prompt"), "input": a.get("input"),
                        "raw_output": a.get("raw_output"), "output": a.get("output")} for a in agents],
            "tool_calls": [{"tool": c["tool"], "params": _redact(c.get("params"), redact_by_tool.get(c["tool_id"], set())),
                            "response": _redact(c.get("response"), redact_by_tool.get(c["tool_id"], set())),
                            "correction": c.get("correction")}
                           for c in trace.get("tool_calls") or []],
        }
    return record


class DashboardSink(TraceSink):
    """Sends a metadata copy of each trace to the bastion service.

    ``export`` only queues the record, so a run never waits on the network.
    A background task posts batches to ``{url}/v1/traces`` every
    *flush_interval* seconds, or as soon as *batch_size* records are queued.

    - 429: waits for Retry-After before trying again.
    - Network error or 5xx: keeps the records and retries on the next flush.
      At most 500 are kept; past that the oldest are dropped, with a warning.
    - 401/403: the key is bad. Logs once, drops the queue, and stops sending.
    - Other 4xx: that batch can never succeed; it's dropped with an error.

    By default only metadata leaves the app (see ``dashboard_record``).
    ``close()`` sends whatever is still queued.
    """

    MAX_BUFFER = 500

    def __init__(
        self,
        url: str,
        api_key: str,
        *,
        include_content: bool = False,
        flush_interval: float = 5.0,
        batch_size: int = 20,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self._key = api_key
        self.include_content = include_content
        self.flush_interval = flush_interval
        self.batch_size = max(1, min(int(batch_size), 100))
        self._timeout = timeout
        self._transport = transport
        self._buffer: deque[dict[str, Any]] = deque()
        self._task: asyncio.Task | None = None
        self._wake: asyncio.Event | None = None
        self._flush_lock: asyncio.Lock | None = None
        self._retry_at = 0.0  # monotonic time before which we don't post (429)
        self._failing = False
        self.disabled = False
        self.dropped = 0
        self.sent = 0

    def __repr__(self) -> str:
        return f"DashboardSink({self.url!r}, include_content={self.include_content})"

    @property
    def pending(self) -> int:
        return len(self._buffer)

    async def export(self, trace: dict[str, Any]) -> None:
        if self.disabled:
            return
        self._buffer.append(dashboard_record(trace, include_content=self.include_content))
        self._trim()
        self._ensure_task()
        if len(self._buffer) >= self.batch_size:
            self._wake.set()

    def _trim(self) -> None:
        over = len(self._buffer) - self.MAX_BUFFER
        if over > 0:
            for _ in range(over):
                self._buffer.popleft()
            self.dropped += over
            log.warning("[trace] DashboardSink queue is full (%d); dropped the %d oldest trace(s), %d so far",
                        self.MAX_BUFFER, over, self.dropped)

    def _ensure_task(self) -> None:
        if self._task is None or self._task.done():
            # Made here, inside the running loop the platform uses.
            self._wake = asyncio.Event()
            self._flush_lock = asyncio.Lock()
            self._task = asyncio.create_task(self._run(), name="bastion-dashboard-sink")

    async def _run(self) -> None:
        while not self.disabled:
            by_size = True
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.flush_interval)
            except asyncio.TimeoutError:
                by_size = False
            self._wake.clear()
            try:
                await self.flush(full_batches_only=by_size)
            except Exception:  # never let the loop die
                log.exception("[trace] DashboardSink flush failed")

    async def flush(self, *, full_batches_only: bool = False) -> None:
        """Send what's queued, batch by batch, until the queue is empty (or,
        with *full_batches_only*, shorter than a batch) or the service says
        to stop for now."""
        if self._flush_lock is None:
            self._flush_lock = asyncio.Lock()
        async with self._flush_lock:
            async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
                while self._buffer and not self.disabled:
                    if full_batches_only and len(self._buffer) < self.batch_size:
                        return
                    if time.monotonic() < self._retry_at:
                        return
                    batch = [self._buffer.popleft() for _ in range(min(self.batch_size, len(self._buffer)))]
                    if not await self._post(client, batch):
                        return

    async def _post(self, client: httpx.AsyncClient, batch: list[dict[str, Any]]) -> bool:
        """True if the batch is done with (sent or unsendable); False to stop
        this flush, with the batch put back at the front of the queue."""
        def put_back() -> None:
            self._buffer.extendleft(reversed(batch))
            self._trim()

        try:
            resp = await client.post(f"{self.url}/v1/traces", json=batch,
                                     headers={"Authorization": f"Bearer {self._key}"})
        except httpx.HTTPError as e:
            put_back()
            if not self._failing:
                log.warning("[trace] can't reach %s (%r); keeping %d trace(s) to retry", self.url, e, len(self._buffer))
            self._failing = True
            return False

        if resp.status_code < 300:
            if self._failing:
                log.info("[trace] %s is reachable again", self.url)
            self._failing = False
            self.sent += len(batch)
            try:
                body = resp.json()
            except ValueError:
                body = {}
            for r in body.get("rejected") or []:
                log.warning("[trace] the service rejected trace %s: %s", r.get("trace_id"), r.get("error"))
            return True
        if resp.status_code == 429:
            put_back()
            try:
                wait = float(resp.headers.get("retry-after", "") or self.flush_interval)
            except ValueError:
                wait = self.flush_interval
            self._retry_at = time.monotonic() + max(wait, 0.0)
            log.warning("[trace] rate limited by %s; retrying in %.0fs", self.url, wait)
            return False
        if resp.status_code in (401, 403):
            self.disabled = True
            dropped = len(batch) + len(self._buffer)
            self._buffer.clear()
            log.error("[trace] %s rejected the app key (%s); DashboardSink stopped, %d trace(s) not sent. "
                      "Local traces are unaffected.", self.url, resp.status_code, dropped)
            return False
        if resp.status_code >= 500:
            put_back()
            if not self._failing:
                log.warning("[trace] %s answered %s; keeping %d trace(s) to retry", self.url, resp.status_code, len(self._buffer))
            self._failing = True
            return False
        log.error("[trace] %s refused a batch of %d (%s: %s); dropped it", self.url, len(batch),
                  resp.status_code, resp.text[:300])
        return True

    async def close(self) -> None:
        """Stop the background task and send what's queued (one attempt;
        a 429 wait is not honoured at shutdown beyond one try)."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._buffer and not self.disabled:
            self._retry_at = 0.0
            try:
                await self.flush()
            except Exception:
                log.exception("[trace] DashboardSink final flush failed")
        if self._buffer:
            log.warning("[trace] DashboardSink closed with %d trace(s) unsent", len(self._buffer))


class TraceExporter:
    """Fans a trace out to every sink. Never raises, never hangs a run."""

    def __init__(self, sinks: list[TraceSink], *, timeout: float = 2.0) -> None:
        self.sinks = sinks
        self.timeout = timeout

    async def export(self, trace: dict[str, Any]) -> None:
        for sink in self.sinks:
            try:
                await asyncio.wait_for(sink.export(trace), timeout=self.timeout)
            except Exception as exc:  # TimeoutError included
                log.warning("[trace] %s failed for trace %s: %r", type(sink).__name__, trace.get("trace_id"), exc)

    async def close(self) -> None:
        """Let sinks that buffer (DashboardSink) send what they hold."""
        for sink in self.sinks:
            close = getattr(sink, "close", None)
            if close is not None:
                try:
                    await close()
                except Exception:
                    log.exception("[trace] closing %s failed", type(sink).__name__)
