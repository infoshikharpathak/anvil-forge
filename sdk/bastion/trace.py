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
- DashboardSink: interface only until Phase 4c. Metadata only by
  default; content is opt-in and will be redacted.

TraceExporter never lets a sink block or fail a run: each export has a
timeout and every error is logged and swallowed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_forge.core.hooks import AgentHooks, HookDecision

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

    def saw(self, agent: str) -> None:
        if agent not in self.agents_seen:
            self.agents_seen.append(agent)


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
    def on_tool_call(self, ctx: RunContext, name: str, tool_id: str, args: dict, response: dict) -> None:
        c = self.for_ctx(ctx)
        if c is None:
            return
        correction = response.get("correction") or {}
        c.tool_calls.append({
            "agent": ctx.agent_name,
            "tool": name,
            "tool_id": tool_id,
            "params": args,
            "success": response.get("success"),
            "tool_executed": response.get("tool_executed"),
            "violation": correction.get("violation"),
            "correction": correction.get("message"),
            "required_actions": correction.get("required_actions") or [],
            "response": response.get("data"),
        })

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
        return HookDecision.allow()

    async def after_agent_run(self, ctx, output):
        c = self._collectors.for_ctx(ctx)
        if c is not None:
            c.raw_outputs.setdefault(ctx.agent_name, []).append(output)
        return HookDecision.allow()


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
        "started_at": collector.started_at,
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "outcome": outcome,
        "error": error,
        "input": collector.message,
        "output": output,
        "agents": agents,
        "tool_calls": collector.tool_calls,
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


class DashboardSink(TraceSink):
    """Sends traces to the bastion dashboard. Interface only: ships in
    Phase 4c (batched, async, with a local fallback file).

    By default only metadata leaves the app: IDs, agents, prompt versions,
    models, tokens, cost, latency, statuses and violation types. Prompts,
    inputs, outputs and tool params are sent only with
    ``include_content=True``, and are redacted even then.
    """

    def __init__(self, url: str | None = None, api_key: str | None = None, *, include_content: bool = False) -> None:
        self.url = url
        self.api_key = api_key
        self.include_content = include_content

    async def export(self, trace: dict[str, Any]) -> None:
        raise NotImplementedError("DashboardSink is implemented in Phase 4c.")


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
