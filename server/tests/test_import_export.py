"""Item 6: import garrison YAML into an app, export it back."""

from pathlib import Path

import yaml
from garrison import load_config
from fastapi.testclient import TestClient

IRONCLAD = Path(__file__).parent / "fixtures" / "anvil-ironclad.yaml"  # copy of Ironclad@32b75d8


def import_yaml(client, text, app="ironclad"):
    return client.post(f"/v1/apps/{app}/import", content=text.encode(),
                       headers={"Content-Type": "application/yaml"})


def same_compiled(a, b):
    assert a.platform.model_dump() == b.platform.model_dump()
    assert a.anvil.model_dump() == b.anvil.model_dump()


def test_ironclad_yaml_round_trips_through_the_service_identically(ana):
    client, app = ana
    r = import_yaml(client, IRONCLAD.read_text())
    assert r.status_code == 200, r.text
    assert r.json()["created"] is True and r.json()["skipped_agents"] == []
    raw = client.post("/v1/apps/ironclad/keys", json={}).json()["key"]
    sdk = TestClient(app).get("/v1/sdk/bundle", headers={"Authorization": f"Bearer {raw}"})
    served = load_config(sdk.json()["bundle"])
    same_compiled(served, load_config(IRONCLAD))
    agent = served.platform.agents["ironclad.banking_agent"]
    assert agent.prompt_version == "v1" and "Ironclad Bank AI Assistant" in agent.instructions
    assert served.anvil.tools["ironclad.initiate_transfer"].redact_fields == ["otp_code"]


def test_reimporting_the_same_file_changes_nothing(ana):
    client, _ = ana
    first = import_yaml(client, IRONCLAD.read_text()).json()["revision"]
    again = import_yaml(client, IRONCLAD.read_text()).json()
    assert again["revision"] == first and again["created"] is False
    versions = client.get("/v1/apps/ironclad/agents/banking_agent/versions").json()
    assert [v["label"] for v in versions] == ["v1"]


def test_changed_prompt_becomes_a_new_version_with_history_kept(ana):
    client, _ = ana
    import_yaml(client, IRONCLAD.read_text())
    doc = yaml.safe_load(IRONCLAD.read_text())
    doc["agents"]["ironclad.banking_agent"].update(instructions="Be brief and careful.", prompt_version="v2")
    r = import_yaml(client, yaml.safe_dump(doc))
    assert r.status_code == 200
    versions = client.get("/v1/apps/ironclad/agents/banking_agent/versions").json()
    assert [(v["label"], v["active"]) for v in versions] == [("v2", True), ("v1", False)]


def test_export_is_importable_and_identical(ana):
    client, _ = ana
    import_yaml(client, IRONCLAD.read_text())
    exported = client.get("/v1/apps/ironclad/export")
    assert exported.headers["content-type"].startswith("application/yaml")
    assert 'filename="ironclad.garrison.yaml"' in exported.headers["content-disposition"]
    same_compiled(load_config(yaml.safe_load(exported.text)), load_config(IRONCLAD))
    rev = client.get("/v1/apps/ironclad").json()["revision"]
    assert import_yaml(client, exported.text).json()["revision"] == rev  # no-op


def test_bad_imports_are_rejected_with_reasons(ana):
    client, _ = ana
    assert import_yaml(client, "apps: [unclosed").status_code == 422
    assert "no app named 'ironclad'" in import_yaml(client, "apps: {other: {}}").json()["detail"]
    doc = yaml.safe_load(IRONCLAD.read_text())
    doc["agents"]["ironclad.banking_agent"]["tools"].append("teleport")
    r = import_yaml(client, yaml.safe_dump(doc))
    assert r.status_code == 422 and "teleport" in r.json()["detail"]
    doc = yaml.safe_load(IRONCLAD.read_text())
    doc["apps"]["ironclad"]["tools"]["get_accounts"]["api_key"] = "sk-live-123"
    assert import_yaml(client, yaml.safe_dump(doc)).status_code == 422
    assert client.get("/v1/apps/ironclad").status_code == 404  # nothing half-created


def test_grants_need_the_granting_app_first(ana):
    client, _ = ana
    doc = yaml.safe_load(IRONCLAD.read_text())
    doc["apps"]["notify"] = {"base_url": "http://notify.test", "tools": {"send_email": {"path": "/email", "trust": "trusted"}}}
    doc["grants"] = [{"tool": "notify.send_email", "to_apps": ["ironclad"]}]
    doc["agents"]["ironclad.banking_agent"]["tools"].append("notify.send_email")
    r = import_yaml(client, yaml.safe_dump(doc))
    assert r.status_code == 422 and "create app 'notify'" in r.json()["detail"]
    client.post("/v1/apps", json={"name": "notify", "base_url": "http://notify.test"})
    client.post("/v1/apps/notify/tools", json={"name": "send_email", "path": "/email", "trust": "trusted"})
    assert import_yaml(client, yaml.safe_dump(doc)).status_code == 200
    assert client.get("/v1/apps/ironclad/grants").json()[0]["tool"] == "notify.send_email"
