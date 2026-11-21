"""Item 3: the management API. Every write is compiled with the SDK, revisioned and audited."""

from anvil_forge import load_config
from sqlalchemy import func, select

from anvil_forge_server.models import Agent, AuditLog, BundleRevision, PromptVersion

from .conftest import AGENT, TOOL, as_other_user, make_ironclad


def revision(client, app="ironclad"):
    return client.get(f"/v1/apps/{app}").json()["revision"]


def bundle(client, app="ironclad"):
    return client.get(f"/v1/apps/{app}/bundle").json()["bundle"]


def test_bundle_is_the_sdk_config_and_compiles(ana):
    client, _ = ana
    make_ironclad(client)
    b = bundle(client)
    assert b["apps"]["ironclad"]["tools"]["get_accounts"]["path"] == "/accounts"
    agent = b["agents"]["ironclad.banking_agent"]
    assert agent["instructions"] == AGENT["instructions"] and agent["prompt_version"] == "v1"
    compiled = load_config(b)
    assert compiled.agent_tools("ironclad.banking_agent") == ["get_accounts"]
    assert revision(client) == 3  # app, tool, agent


def test_every_write_needs_a_session_and_csrf(ana, make_client):
    client, _ = ana
    anon, _ = make_client()
    assert anon.get("/v1/apps").status_code == 401
    no_csrf = client.post("/v1/apps", json={"name": "x"}, headers={"X-Anvil-Forge": ""})
    assert no_csrf.status_code == 403


def test_editing_instructions_creates_an_immutable_version_and_rollback_moves_the_pointer(ana):
    client, app = ana
    make_ironclad(client)
    r = client.patch("/v1/apps/ironclad/agents/banking_agent", json={"instructions": "Be concise."})
    assert r.json()["prompt_version"] == "v2"
    assert bundle(client)["agents"]["ironclad.banking_agent"]["instructions"] == "Be concise."
    # same instructions again: no new version, no new revision
    rev = revision(client)
    client.patch("/v1/apps/ironclad/agents/banking_agent", json={"instructions": "Be concise."})
    assert revision(client) == rev
    versions = client.get("/v1/apps/ironclad/agents/banking_agent/versions").json()
    assert [(v["label"], v["active"]) for v in versions] == [("v2", True), ("v1", False)]

    back = client.post("/v1/apps/ironclad/agents/banking_agent/rollback", json={"version": 1})
    assert back.json()["prompt_version"] == "v1"
    assert bundle(client)["agents"]["ironclad.banking_agent"]["instructions"] == AGENT["instructions"]
    after = client.get("/v1/apps/ironclad/agents/banking_agent/versions").json()
    assert [(v["label"], v["instructions"], v["active"]) for v in after] == [
        ("v2", "Be concise.", False), ("v1", AGENT["instructions"], True)]  # nothing rewritten
    assert client.post("/v1/apps/ironclad/agents/banking_agent/rollback", json={"version": 9}).status_code == 404
    # model/tool edits don't create prompt versions
    client.patch("/v1/apps/ironclad/agents/banking_agent", json={"model": "gpt-5.4-mini"})
    with app.state.sessionmaker() as db:
        assert db.scalar(select(func.count()).select_from(PromptVersion)) == 2


def test_invalid_write_is_rejected_with_the_compiler_error_and_saves_nothing(ana):
    client, app = ana
    make_ironclad(client)
    rev = revision(client)
    with app.state.sessionmaker() as db:
        audits = db.scalar(select(func.count()).select_from(AuditLog))
    bad = client.post("/v1/apps/ironclad/agents", json={**AGENT, "name": "rogue", "tools": ["teleport"]})
    assert bad.status_code == 422
    assert "unregistered tool 'ironclad.teleport'" in bad.json()["detail"]
    with app.state.sessionmaker() as db:
        assert db.scalar(select(Agent).where(Agent.name == "rogue")) is None
        assert db.scalar(select(func.count()).select_from(AuditLog)) == audits
    assert revision(client) == rev
    # deleting a tool an agent uses is also rejected
    gone = client.delete("/v1/apps/ironclad/tools/get_accounts")
    assert gone.status_code == 422 and "get_accounts" in gone.json()["detail"]
    assert client.get("/v1/apps/ironclad/tools/get_accounts").status_code == 200
    # a sequence rule naming a tool the agent doesn't have
    seq = client.patch("/v1/apps/ironclad/agents/banking_agent",
                       json={"sequences": [{"before": "initiate_transfer", "requires": ["get_accounts"]}]})
    assert seq.status_code == 422


def test_no_field_can_hold_a_secret(ana):
    client, _ = ana
    for body in (
        {"name": "x", "auth": {"header": "Authorization", "secret_env": "TOKEN", "secret": "sk-live-123"}},
        {"name": "x", "auth": {"header": "Authorization", "secret_env": "sk-live-123"}},  # not an env var name
        {"name": "x", "api_key": "sk-live-123"},
    ):
        assert client.post("/v1/apps", json=body).status_code == 422, body
    assert client.post("/v1/apps", json={"name": "ok", "base_url": "http://x"}).status_code == 201
    assert client.post("/v1/apps/ok/tools", json={**TOOL, "api_key": "sk-live-123"}).status_code == 422
    ok = client.post("/v1/apps", json={"name": "authed", "auth": {"header": "Authorization", "secret_env": "BANK_TOKEN", "prefix": "Bearer "}})
    assert ok.json()["auth"] == {"header": "Authorization", "secret_env": "BANK_TOKEN", "prefix": "Bearer "}


def test_names_with_double_underscore_rejected(ana):
    client, _ = ana
    assert client.post("/v1/apps", json={"name": "my__app"}).status_code == 422


def test_grants_put_only_granted_tools_in_the_bundle_and_track_dependents(ana):
    client, _ = ana
    make_ironclad(client)
    client.post("/v1/apps", json={"name": "notify", "base_url": "http://notify.test"})
    client.post("/v1/apps/notify/tools", json={"name": "send_email", "path": "/email", "trust": "trusted"})
    client.post("/v1/apps/notify/tools", json={"name": "send_sms", "path": "/sms", "trust": "trusted"})
    # using it before it's granted fails to compile
    denied = client.patch("/v1/apps/ironclad/agents/banking_agent", json={"tools": ["get_accounts", "notify.send_email"]})
    # not granted, so notify isn't in ironclad's bundle at all
    assert denied.status_code == 422 and "unregistered tool 'notify.send_email'" in denied.json()["detail"]
    g = client.post("/v1/apps/ironclad/grants", json={"tool": "notify.send_email"})
    assert g.status_code == 201
    assert client.patch("/v1/apps/ironclad/agents/banking_agent",
                        json={"tools": ["get_accounts", "notify.send_email"]}).status_code == 200
    b = bundle(client)
    assert b["apps"]["notify"] == {"base_url": "http://notify.test", "tools": {"send_email": b["apps"]["notify"]["tools"]["send_email"]}}
    assert b["grants"] == [{"tool": "notify.send_email", "to_apps": ["ironclad"]}]
    load_config(b)

    # changing the granted tool in notify gives ironclad a new revision too
    rev = revision(client)
    client.put("/v1/apps/notify/tools/send_email", json={"path": "/v2/email", "trust": "trusted"})
    assert revision(client) == rev + 1
    assert bundle(client)["apps"]["notify"]["tools"]["send_email"]["path"] == "/v2/email"
    # an un-granted notify tool change doesn't touch ironclad
    client.put("/v1/apps/notify/tools/send_sms", json={"path": "/v2/sms", "trust": "trusted"})
    assert revision(client) == rev + 1
    # removing what an agent relies on is rejected
    assert client.delete(f"/v1/apps/ironclad/grants/{g.json()['id']}").status_code == 422
    assert client.delete("/v1/apps/notify/tools/send_email").status_code == 422
    assert client.delete("/v1/apps/notify").status_code == 422
    assert client.post("/v1/apps/ironclad/grants", json={"tool": "ironclad.get_accounts"}).status_code == 422


def test_another_users_apps_are_404(ana, make_client):
    client, _ = ana
    make_ironclad(client)
    bob = as_other_user(make_client)
    for path in ("/v1/apps/ironclad", "/v1/apps/ironclad/bundle", "/v1/apps/ironclad/tools",
                 "/v1/apps/ironclad/agents/banking_agent", "/v1/apps/ironclad/agents/banking_agent/versions",
                 "/v1/apps/ironclad/grants", "/v1/apps/ironclad/revisions"):
        assert bob.get(path).status_code == 404, path
    assert bob.patch("/v1/apps/ironclad/agents/banking_agent", json={"instructions": "pwned"}).status_code == 404
    assert bob.delete("/v1/apps/ironclad").status_code == 404
    assert bob.get("/v1/apps").json() == []
    assert bob.get("/v1/audit").json() == []
    # bob's own app can't take a grant from ana's
    bob.post("/v1/apps", json={"name": "bobapp"})
    assert bob.post("/v1/apps/bobapp/grants", json={"tool": "ironclad.get_accounts"}).status_code == 404
    # and both can have an app named the same
    assert bob.post("/v1/apps", json={"name": "ironclad"}).status_code == 201


def test_every_successful_write_is_audited(ana):
    client, _ = ana
    make_ironclad(client)
    client.patch("/v1/apps/ironclad/agents/banking_agent", json={"instructions": "Be concise."})
    log = client.get("/v1/audit", params={"app": "ironclad"}).json()
    assert [(e["action"], e["entity_type"], e["entity_name"]) for e in log] == [
        ("update", "agent", "ironclad.banking_agent"),
        ("create", "agent", "ironclad.banking_agent"),
        ("create", "tool", "ironclad.get_accounts"),
        ("create", "app", "ironclad"),
    ]
    update = log[0]
    assert update["before"]["prompt_version"] == "v1" and update["after"]["prompt_version"] == "v2"
    assert all(e["created_at"] for e in log)


def test_revisions_are_monotonic_and_skip_no_ops(ana):
    client, app = ana
    make_ironclad(client)
    client.patch("/v1/apps/ironclad", json={"settings": {"injection_scanning": True}})
    client.patch("/v1/apps/ironclad", json={"settings": {"injection_scanning": True}})  # no change
    revs = client.get("/v1/apps/ironclad/revisions").json()
    assert [r["revision"] for r in revs] == [4, 3, 2, 1]
    with app.state.sessionmaker() as db:
        assert len({r.etag for r in db.scalars(select(BundleRevision))}) == 4
