"""4c item 2: DashboardSink (batching, flushing, retries) and the record it sends."""

import asyncio
import json
import logging
import uuid

import httpx
import pytest

from bastion.trace import REDACTED, DashboardSink, TraceExporter, dashboard_record

URL = "http://bastion.test"
KEY = "bk_" + "k" * 43


def full_trace(**overrides):
    """A full (local) trace, as build_trace returns it."""
    t = {
        "trace_id": str(uuid.uuid4()), "run_id": uuid.uuid4().hex, "session_id": "chat-1", "bundle_revision": 4,
        "app": "ironclad", "kind": "agent", "target": "ironclad.banking_agent", "flow_entry": None,
        "started_at": "2026-11-22T10:00:00+00:00", "ended_at": "2026-11-22T10:00:02+00:00",
        "outcome": "success", "error": None,
        "input": "Send $5 to Bo", "output": "Started transfer T-1. Confirm the OTP in the dashboard.",
        "agents": [{"agent": "ironclad.banking_agent", "registered": True, "prompt_version": "v2",
                    "backend": "openai", "model": "gpt-4o-mini", "status": "success",
                    "input_tokens": 100, "output_tokens": 20, "cost_usd": 0.0001, "latency_ms": 1800.0,
                    "prompt": "You are the Ironclad Bank AI Assistant.", "input": "Send $5 to Bo",
                    "raw_output": "Started transfer T-1.", "output": "Started transfer T-1."}],
        "tool_calls": [{
            "agent": "ironclad.banking_agent", "tool": "initiate_transfer", "tool_id": "ironclad.initiate_transfer",
            "tool_app": "ironclad", "params": {"user_id": "demo_user", "amount": 5, "pin": "1234"},
            "success": True, "tool_executed": True, "status": "allowed", "violation": None, "correction": None,
            "required_actions": [], "response": {"transfer_id": "T-1", "otp_code": "999111", "status": "pending"},
            "started_ms": 300.0, "duration_ms": 45.0, "redacted_fields": ["otp_code"],
        }],
        "steps": [{"type": "agent_start", "agent": "ironclad.banking_agent", "offset_ms": 0.0},
                  {"type": "agent_end", "agent": "ironclad.banking_agent", "offset_ms": 1800.0,
                   "duration_ms": 1800.0, "status": "success"}],
        "violations": [], "hook_denials": [],
        "scans": {"calls": 1, "input_tokens": 7, "output_tokens": 3, "cost_usd": 0.00001,
                  "detail": [{"agent": "ironclad.banking_agent", "provider": "openai", "model": "gpt-4o-mini",
                              "input_tokens": 7, "output_tokens": 3, "cost_usd": 0.00001,
                              "detected": False, "pattern": None}]},
        "totals": {"input_tokens": 107, "output_tokens": 23, "cost_usd": 0.00011, "latency_ms": 1850.0},
    }
    t.update(overrides)
    return t


class Service:
    """A fake /v1/traces: answers from a script, records every batch."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.batches: list[list[dict]] = []
        self.auth: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/traces" and request.method == "POST"
        self.auth.append(request.headers["authorization"])
        answer = self.answers.pop(0) if self.answers else 202
        if isinstance(answer, Exception):
            raise answer
        status, headers = (answer, {}) if isinstance(answer, int) else answer
        body = json.loads(request.content)
        if status < 300:
            self.batches.append(body)
            return httpx.Response(status, json={"accepted": len(body), "duplicates": 0, "rejected": []})
        return httpx.Response(status, headers=headers, json={"detail": "no"})

    def sink(self, **kw):
        kw.setdefault("flush_interval", 60)  # tests flush explicitly unless they test the timer
        return DashboardSink(URL, KEY, transport=httpx.MockTransport(self.handler), **kw)

    @property
    def sent(self):
        return [r for b in self.batches for r in b]


# ── the record ────────────────────────────────────────────────────────────────

def test_record_is_metadata_only_by_default():
    rec = dashboard_record(full_trace())
    assert rec["content"] is None
    assert (rec["agent"], rec["status"], rec["model"], rec["prompt_version"]) == (
        "ironclad.banking_agent", "success", "gpt-4o-mini", "v2")
    assert (rec["token_input"], rec["token_output"], rec["duration_ms"]) == (107, 23, 1850.0)
    assert rec["tool_calls"] == [{"tool": "initiate_transfer", "tool_id": "ironclad.initiate_transfer",
                                  "app": "ironclad", "agent": "ironclad.banking_agent", "status": "allowed",
                                  "violation": None, "started_ms": 300.0, "duration_ms": 45.0,
                                  "redacted_fields": ["otp_code"]}]
    text = json.dumps(rec)
    for secret in ("Send $5", "demo_user", "999111", "1234", "Ironclad Bank AI", "T-1"):
        assert secret not in text, secret
    assert rec["scans"][0] == {"source": "classifier", "agent": "ironclad.banking_agent", "model": "gpt-4o-mini",
                               "detected": False, "pattern": None, "input_tokens": 7, "output_tokens": 3}


def test_content_is_opt_in_and_redacted():
    rec = dashboard_record(full_trace(), include_content=True)
    c = rec["content"]
    assert c["input"] == "Send $5 to Bo" and c["agents"][0]["prompt"].startswith("You are")
    call = c["tool_calls"][0]
    assert call["params"] == {"user_id": "demo_user", "amount": 5, "pin": REDACTED}
    assert call["response"]["otp_code"] == REDACTED and call["response"]["transfer_id"] == "T-1"


def test_status_mapping_and_injection_scans():
    blocked = dashboard_record(full_trace(
        outcome="blocked", hook_denials=[{"guardrail": "hook"}],
        violations=[{"agent": "ironclad.banking_agent", "kind": "agent_input", "tool": None, "target_agent": None,
                     "violation": "injection_detected_agent_input",
                     "message": "The input matched injection patterns ('ignore previous') and was blocked."}]))
    assert blocked["status"] == "blocked" and blocked["hook_denials"] == 1
    assert blocked["violations"][0] == {"agent": "ironclad.banking_agent", "kind": "agent_input", "tool": None,
                                        "target_agent": None, "violation": "injection_detected_agent_input"}
    assert blocked["scans"][-1] == {"source": "anvil", "agent": "ironclad.banking_agent", "detected": True,
                                    "violation": "injection_detected_agent_input", "tool": None,
                                    "pattern": "ignore previous"}
    assert dashboard_record(full_trace(outcome="withheld"))["status"] == "blocked"
    assert dashboard_record(full_trace(outcome="failed", error="RuntimeError()"))["status"] == "error"


def test_flow_record_names_the_flow_and_entry_agent():
    rec = dashboard_record(full_trace(kind="flow", target="ironclad.disputes", flow_entry="ironclad.triage"))
    assert (rec["kind"], rec["flow"], rec["agent"]) == ("flow", "ironclad.disputes", "ironclad.triage")


# ── the sink ──────────────────────────────────────────────────────────────────

async def test_batches_by_size():
    svc = Service()
    sink = svc.sink(batch_size=3)
    for _ in range(7):
        await sink.export(full_trace())
    await asyncio.sleep(0.05)  # size reached: the background task flushed without waiting for the timer
    assert [len(b) for b in svc.batches] == [3, 3]
    assert sink.pending == 1
    await sink.close()  # sends the rest
    assert [len(b) for b in svc.batches] == [3, 3, 1] and sink.sent == 7
    assert svc.auth == [f"Bearer {KEY}"] * 3


async def test_flushes_on_interval():
    svc = Service()
    sink = svc.sink(batch_size=20, flush_interval=0.05)
    await sink.export(full_trace())
    assert svc.batches == []
    await asyncio.sleep(0.2)
    assert [len(b) for b in svc.batches] == [1] and sink.pending == 0
    await sink.close()


async def test_export_never_waits_for_the_network():
    async def slow(request):
        await asyncio.sleep(5)
        return httpx.Response(202, json={})

    sink = DashboardSink(URL, KEY, transport=httpx.MockTransport(slow), batch_size=1, flush_interval=60)
    exporter = TraceExporter([sink], timeout=0.05)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await exporter.export(full_trace())  # queues; the post happens in the background
    assert loop.time() - t0 < 0.05
    sink._task.cancel()


async def test_retries_after_5xx_and_network_errors(caplog):
    svc = Service(503, httpx.ConnectError("down"), 202)
    sink = svc.sink(batch_size=2)
    for _ in range(2):
        await sink.export(full_trace())
    await sink.flush()
    assert sink.pending == 2 and svc.batches == []  # kept after the 503
    await sink.flush()
    assert sink.pending == 2  # kept after the network error
    await sink.flush()
    assert sink.pending == 0 and len(svc.sent) == 2
    assert "answered 503" in caplog.text
    await sink.close()


async def test_order_is_kept_across_a_retry():
    svc = Service(500)
    sink = svc.sink(batch_size=2)
    traces = [full_trace() for _ in range(3)]
    for t in traces:
        await sink.export(t)
    await sink.flush()  # first batch fails and goes back to the front
    await sink.flush()
    assert [r["trace_id"] for r in svc.sent] == [t["trace_id"] for t in traces]
    await sink.close()


async def test_drops_oldest_when_the_buffer_overflows(caplog):
    svc = Service(*([503] * 10))
    sink = svc.sink(batch_size=100)
    sink.MAX_BUFFER = 5
    traces = [full_trace() for _ in range(8)]
    with caplog.at_level(logging.WARNING):
        for t in traces:
            await sink.export(t)
    assert sink.pending == 5 and sink.dropped == 3
    assert "dropped the 1 oldest" in caplog.text
    svc.answers = []
    await sink.flush()
    assert [r["trace_id"] for r in svc.sent] == [t["trace_id"] for t in traces[3:]]
    await sink.close()


async def test_honours_retry_after_on_429():
    svc = Service((429, {"Retry-After": "1"}), 202)
    sink = svc.sink(batch_size=5)
    await sink.export(full_trace())
    await sink.flush()
    assert sink.pending == 1
    await sink.flush()  # still inside Retry-After: doesn't post
    assert len(svc.auth) == 1
    await asyncio.sleep(1.05)
    await sink.flush()
    assert sink.pending == 0 and len(svc.sent) == 1
    await sink.close()


@pytest.mark.parametrize("status", [401, 403])
async def test_bad_key_stops_the_sink(status, caplog):
    svc = Service(status)
    sink = svc.sink(batch_size=1)
    with caplog.at_level(logging.ERROR):
        await sink.export(full_trace())
        await sink.flush()
        for _ in range(3):
            await sink.export(full_trace())
            await sink.flush()
    assert sink.disabled and sink.pending == 0
    assert len(svc.auth) == 1  # never tried again
    assert caplog.text.count("rejected the app key") == 1
    await sink.close()


async def test_unsendable_batch_is_dropped_not_retried(caplog):
    svc = Service(422, 202)
    sink = svc.sink(batch_size=1)
    await sink.export(full_trace())
    await sink.export(full_trace())
    await sink.flush()
    assert len(svc.sent) == 1 and sink.pending == 0  # the 422 batch is gone, the next one went
    assert "refused a batch" in caplog.text
    await sink.close()


async def test_rejected_records_are_logged(caplog):
    def handler(request):
        body = json.loads(request.content)
        return httpx.Response(202, json={"accepted": 0, "duplicates": 0, "rejected": [
            {"index": 0, "trace_id": body[0]["trace_id"], "error": "agent 'x' is not registered"}]})

    sink = DashboardSink(URL, KEY, transport=httpx.MockTransport(handler), flush_interval=60)
    await sink.export(full_trace())
    await sink.flush()
    assert "is not registered" in caplog.text
    await sink.close()
