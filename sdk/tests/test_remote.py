"""Item 7: Platform.from_service: bundle from the service, disk cache,
ETag refresh, atomic swap, sessions carried across a swap."""

import asyncio
import copy
import hashlib
import json

import httpx
import pytest

from bastion import LocalSink, Platform, PlatformError

from .conftest import BASE, base_config
from .llm import ScriptedOpenAI
from .upstream import Upstream

KEY = "bk_test_" + "x" * 34
SUPPORT = BASE["agents"]["ironclad.support_agent"]["instructions"]
URL = "http://bastion.test"


@pytest.fixture(autouse=True)
def _clean_registry():
    yield
    from agent_forge.tools import _REGISTRY, sources

    for name in [n for n, fn in _REGISTRY.items() if getattr(fn, "_tool_source", None) is not None]:
        _REGISTRY.pop(name)
    sources._SOURCE_TOOLS.clear()


class FakeService:
    """GET /v1/sdk/bundle with key auth and ETags, like bastion-server."""

    def __init__(self, bundle, revision=1):
        self.up = True
        self.calls = []
        self.publish(bundle, revision)

    def publish(self, bundle, revision):
        self.bundle, self.revision = copy.deepcopy(bundle), revision
        self.etag = hashlib.sha256(json.dumps(self.bundle, sort_keys=True).encode()).hexdigest()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if not self.up:
            raise httpx.ConnectError("service down")
        if request.headers.get("authorization") != f"Bearer {KEY}":
            return httpx.Response(401, json={"detail": "A valid app key is required."})
        if request.headers.get("if-none-match", "").strip('"') == self.etag:
            return httpx.Response(304)
        return httpx.Response(200, headers={"ETag": f'"{self.etag}"'},
                              json={"app": "ironclad", "revision": self.revision, "etag": self.etag, "bundle": self.bundle})


def edited(instructions="You are Ironclad's support agent, version two.", label="v4"):
    b = base_config()
    b["agents"]["ironclad.support_agent"].update(instructions=instructions, prompt_version=label)
    return b


def platform(service, tmp_path, llm=None, **kw):
    return Platform.from_service(URL, KEY, cache_dir=tmp_path / "cache", refresh_interval=0,
                                 service_transport=httpx.MockTransport(service),
                                 openai_client=llm, http_transport=Upstream().transport(), **kw)


def test_app_comes_from_the_key_and_the_cache_holds_no_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("IRONCLAD_TOKEN", "tok-secret-value")
    svc = FakeService(base_config())
    p = platform(svc, tmp_path)
    assert p.app == "ironclad" and p.bundle_revision == 1
    (cache,) = (tmp_path / "cache").iterdir()
    text = cache.read_text()
    assert KEY not in text and "tok-secret-value" not in text
    assert json.loads(text)["revision"] == 1 and "IRONCLAD_TOKEN" in text  # env var NAME only


async def test_runs_record_the_bundle_revision(tmp_path, monkeypatch):
    monkeypatch.setenv("IRONCLAD_TOKEN", "tok")
    llm = ScriptedOpenAI({SUPPORT: [{"text": "hello"}]})
    p = platform(FakeService(base_config(), revision=7), tmp_path, llm)
    reply = await p.run_agent("support_agent", "hi", trace_id="rev-1")
    assert reply.trace["bundle_revision"] == 7 and LocalSink().read("rev-1")[0]["bundle_revision"] == 7
    await p.close()


def test_service_down_at_startup_uses_the_cache(tmp_path):
    svc = FakeService(base_config(), revision=3)
    platform(svc, tmp_path)  # populates the cache
    svc.up = False
    p = platform(svc, tmp_path)
    assert p.bundle_revision == 3 and p._remote.from_cache


def test_service_down_and_no_cache_is_a_clear_error(tmp_path):
    svc = FakeService(base_config())
    svc.up = False
    with pytest.raises(PlatformError) as e:
        platform(svc, tmp_path)
    msg = str(e.value)
    assert URL in msg and "no cached bundle" in msg and str(tmp_path / "cache") in msg


def test_rejected_key_fails_even_with_a_cache(tmp_path, monkeypatch):
    svc = FakeService(base_config())
    platform(svc, tmp_path)
    monkeypatch.setattr("tests.test_remote.KEY", "bk_revoked")  # what the client sends no longer matches
    with pytest.raises(PlatformError, match="rejected the app key"):
        Platform.from_service(URL, "bk_wrong" + "y" * 30, cache_dir=tmp_path / "cache",
                              service_transport=httpx.MockTransport(svc))


async def test_refresh_uses_etag_and_swaps_in_a_new_revision(tmp_path, monkeypatch):
    monkeypatch.setenv("IRONCLAD_TOKEN", "tok")
    v2 = "You are Ironclad's support agent, version two."
    llm = ScriptedOpenAI({SUPPORT: [{"text": "from v3"}], v2: [{"text": "from v4"}]})
    svc = FakeService(base_config())
    p = platform(svc, tmp_path, llm)
    await p.start()
    assert await p.refresh() is False  # 304
    assert svc.calls[-1].headers["if-none-match"].strip('"') == svc.etag
    first = await p.run_agent("support_agent", "hi")
    svc.publish(edited(v2), revision=2)
    assert await p.refresh() is True and p.bundle_revision == 2
    second = await p.run_agent("support_agent", "hi")
    assert (first.content, first.trace["agents"][0]["prompt_version"]) == ("from v3", "v3")
    assert (second.content, second.trace["agents"][0]["prompt_version"]) == ("from v4", "v4")
    assert second.trace["bundle_revision"] == 2
    cached = json.loads(next((tmp_path / "cache").iterdir()).read_text())
    assert cached["revision"] == 2  # cache follows the service
    await p.close()


async def test_a_run_in_progress_finishes_on_the_old_config(tmp_path, monkeypatch):
    monkeypatch.setenv("IRONCLAD_TOKEN", "tok")
    v2 = "You are Ironclad's support agent, version two."
    gate, inside = asyncio.Event(), asyncio.Event()

    async def slow(kwargs):
        inside.set()
        await gate.wait()
        return {"text": "old config answer"}

    class Slow(ScriptedOpenAI):
        async def _create(self, **kwargs):
            system = kwargs["messages"][0]["content"]
            if system == SUPPORT:
                self.requests.setdefault(system, []).append(kwargs)
                step = await slow(kwargs)
                return self._completion({"role": "assistant", "content": step["text"]}, "stop")
            return await super()._create(**kwargs)

    llm = Slow({v2: [{"text": "new config answer"}]})
    svc = FakeService(base_config())
    p = platform(svc, tmp_path, llm)
    await p.start()
    running = asyncio.create_task(p.run_agent("support_agent", "first"))
    await inside.wait()                       # mid-run on revision 1
    svc.publish(edited(v2), revision=2)
    assert await p.refresh() is True
    after = await p.run_agent("support_agent", "second")
    gate.set()
    before = await running
    assert (before.content, before.trace["bundle_revision"], before.trace["agents"][0]["prompt_version"]) == (
        "old config answer", 1, "v3")
    assert (after.content, after.trace["bundle_revision"], after.trace["agents"][0]["prompt_version"]) == (
        "new config answer", 2, "v4")
    await p.close()


async def test_tool_sources_are_reregistered(tmp_path, monkeypatch):
    monkeypatch.setenv("IRONCLAD_TOKEN", "tok")
    from agent_forge.tools import list_tools

    svc = FakeService(base_config())
    p = platform(svc, tmp_path)
    await p.start()
    assert "close_account" in list_tools() and "freeze_card" not in list_tools()
    b = base_config()
    b["apps"]["ironclad"]["tools"].pop("close_account")
    b["apps"]["ironclad"]["tools"]["freeze_card"] = {"path": "/freeze", "method": "POST", "trust": "trusted"}
    svc.publish(b, revision=2)
    await p.refresh()
    assert "freeze_card" in list_tools() and "close_account" not in list_tools()
    await p.close()
    assert "freeze_card" not in list_tools()


async def test_open_session_moves_to_the_new_prompt_keeping_history_and_sequence_state(tmp_path, monkeypatch):
    monkeypatch.setenv("IRONCLAD_TOKEN", "tok")
    v2 = "You are Ironclad's support agent, version two."
    llm = ScriptedOpenAI({
        SUPPORT: [{"tool": "get_accounts", "args": {"user_id": "u1"}}, {"text": "You have CHK-1."}],
        v2: [{"tool": "initiate_transfer", "args": {"user_id": "u1", "amount": 5}}, {"text": "Sent."}],
    })
    svc = FakeService(base_config())
    p = platform(svc, tmp_path, llm)
    await p.start()
    await p.run_agent("support_agent", "What are my accounts?", session_id="chat")
    svc.publish(edited(v2), revision=2)
    await p.refresh()
    second = await p.run_agent("support_agent", "Send $5", session_id="chat")
    sent = llm.requests[v2][0]["messages"]
    assert sent[0]["content"] == v2                         # new prompt...
    assert any(m.get("content") == "What are my accounts?" for m in sent if isinstance(m, dict))  # ...same chat
    assert second.trace["tool_calls"][0]["success"] is True  # turn 1's lookup still satisfies the rule
    assert second.trace["agents"][0]["prompt_version"] == "v4"
    await p.close()


async def test_a_bad_bundle_or_an_outage_keeps_the_current_config(tmp_path, monkeypatch):
    monkeypatch.setenv("IRONCLAD_TOKEN", "tok")
    svc = FakeService(base_config())
    p = platform(svc, tmp_path)
    broken = base_config()
    broken["agents"]["ironclad.support_agent"]["tools"].append("teleport")
    svc.publish(broken, revision=2)
    assert await p.refresh() is False and p.bundle_revision == 1
    svc.up = False
    assert await p.refresh() is False and p.bundle_revision == 1


async def test_background_polling_picks_up_changes(tmp_path, monkeypatch):
    monkeypatch.setenv("IRONCLAD_TOKEN", "tok")
    svc = FakeService(base_config())
    p = Platform.from_service(URL, KEY, cache_dir=tmp_path / "cache", refresh_interval=0.01,
                              service_transport=httpx.MockTransport(svc))
    await p.start()
    svc.publish(edited(), revision=5)
    for _ in range(200):
        if p.bundle_revision == 5:
            break
        await asyncio.sleep(0.01)
    assert p.bundle_revision == 5
    await p.close()
