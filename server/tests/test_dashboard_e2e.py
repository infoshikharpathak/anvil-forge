"""4c item 2 against the real service: the SDK's DashboardSink posts runs to
POST /v1/traces, and the app's dashboard options travel in its bundle."""

import json
from types import SimpleNamespace

import httpx
from garrison import Platform
from garrison.classifier import CLASSIFIER_PROMPT
from openai.types.chat import ChatCompletion
from sqlalchemy import select

from garrison_hq.models import Trace

from .test_import_export import IRONCLAD, import_yaml
from .test_remote_e2e import InProcess


class BankModel:
    """Scripted OpenAI client: get_accounts, then an answer. Classifier calls
    get a clean verdict."""

    def __init__(self):
        self.turn = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    @staticmethod
    def _completion(message, finish, tokens=(10, 5)):
        return ChatCompletion.model_validate({
            "id": "c", "object": "chat.completion", "created": 0, "model": "stub",
            "choices": [{"index": 0, "finish_reason": finish, "message": message}],
            "usage": {"prompt_tokens": tokens[0], "completion_tokens": tokens[1], "total_tokens": sum(tokens)},
        })

    async def _create(self, **kw):
        if CLASSIFIER_PROMPT[:60] in kw["messages"][0]["content"]:
            return self._completion({"role": "assistant", "content": '{"injection": false}'}, "stop", (7, 3))
        self.turn += 1
        if self.turn % 2 == 1:
            return self._completion({"role": "assistant", "content": None, "tool_calls": [{
                "id": f"call_{self.turn}", "type": "function",
                "function": {"name": "get_accounts", "arguments": json.dumps({"user_id": "demo_user"})}}]},
                "tool_calls")
        return self._completion({"role": "assistant", "content": "You have one checking account."}, "stop")


def bank(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"accounts": [{"id": "CHK-1", "balance": 1200}]})


def setup(client, app, tmp_path, monkeypatch, imported=False, **kw):
    monkeypatch.setenv("AGENT_FORGE_DB_PATH", str(tmp_path / "forge.db"))
    monkeypatch.chdir(tmp_path)
    if not imported:
        import_yaml(client, IRONCLAD.read_text())
    raw = client.post("/v1/apps/ironclad/keys", json={"name": "agent-service"}).json()["key"]
    p = Platform.from_service("http://testserver", raw, cache_dir=tmp_path / "cache", refresh_interval=0,
                              service_transport=InProcess(app), dashboard_transport=httpx.ASGITransport(app=app),
                              openai_client=BankModel(), http_transport=httpx.MockTransport(bank), **kw)
    return p


def rows(app):
    with app.state.sessionmaker() as db:
        return db.scalars(select(Trace).order_by(Trace.id)).all()


async def test_runs_land_in_the_service_metadata_only(ana, tmp_path, monkeypatch):
    client, app = ana
    p = setup(client, app, tmp_path, monkeypatch)
    first = await p.run_agent("banking_agent", "What are my accounts?", session_id="chat-1")
    second = await p.run_agent("banking_agent", "And again?", session_id="chat-1")
    await p.close()

    stored = rows(app)
    assert [t.trace_id for t in stored] == [first.trace_id, second.trace_id]
    t = stored[0]
    assert (t.agent_name, t.session_id, t.status, t.model) == ("banking_agent", "chat-1", "success", "gpt-4o-mini")
    assert t.bundle_revision == p.bundle_revision and t.prompt_version == "v1" and t.agent_id is not None
    assert t.token_input > 0 and t.duration_ms > 0
    assert [c["tool"] for c in t.tool_calls] == ["get_accounts"] and t.tool_calls[0]["status"] == "allowed"
    assert [s["type"] for s in t.steps][0] == "agent_start" and t.steps[-1]["type"] == "agent_end"
    assert any(s["source"] == "classifier" and s["detected"] is False for s in t.summary["scans"])
    assert t.content is None
    blob = json.dumps([t.tool_calls, t.steps, t.summary])
    assert "What are my accounts" not in blob and "demo_user" not in blob


async def test_content_opt_in_comes_from_the_bundle(ana, tmp_path, monkeypatch):
    client, app = ana
    import_yaml(client, IRONCLAD.read_text())
    assert client.patch("/v1/apps/ironclad", json={"dashboard": {"include_content": True}}).status_code == 200
    bundle = client.get("/v1/apps/ironclad/bundle").json()["bundle"]
    assert bundle["dashboard"] == {"include_content": True}
    p = setup(client, app, tmp_path, monkeypatch, imported=True)
    assert p.dashboard.include_content is True
    result = await p.run_agent("banking_agent", "What are my accounts?")
    await p.close()
    (t,) = rows(app)
    assert t.trace_id == result.trace_id
    assert t.content["input"] == "What are my accounts?"
    assert t.content["tool_calls"][0]["params"] == {"user_id": "demo_user"}


def test_import_keeps_include_content_and_refuses_a_key(ana):
    client, _ = ana
    text = IRONCLAD.read_text()
    with_key = text + "\ndashboard:\n  url: http://localhost:8200\n  key: gk_secret\n  include_content: true\n"
    r = client.post("/v1/apps/ironclad/import", content=with_key, headers={"Content-Type": "application/yaml"})
    assert r.status_code == 422 and "never stores app keys" in r.json()["detail"]
    ok = text + "\ndashboard:\n  url: http://localhost:8200\n  include_content: true\n  flush_interval: 2\n"
    assert client.post("/v1/apps/ironclad/import", content=ok,
                       headers={"Content-Type": "application/yaml"}).status_code == 200
    assert client.get("/v1/apps/ironclad").json()["dashboard"] == {"include_content": True}
    assert "url" not in client.get("/v1/apps/ironclad/bundle").json()["bundle"]["dashboard"]
    bad = client.patch("/v1/apps/ironclad", json={"dashboard": {"include_content": True, "key": "gk_x"}})
    assert bad.status_code == 422
