"""remote_agent tools in the control plane: another app's agent, granted to a
consumer that calls it with its own key (the chat hub -> FoodDash case)."""

from garrison.a2a import CALL_CHAIN_HEADER
from garrison.config import load_config

from .conftest import CSRF  # noqa: F401  (ana already sends it)

AUTH = {"header": "Authorization", "secret_env": "FOODDASH_SERVICE_KEY", "prefix": "Bearer "}
ASK = {"name": "ask_fooddash", "type": "remote_agent", "endpoint": "http://fooddash-agent.test:8100/agent/chat",
       "description": "Ask FoodDash's ordering assistant.", "timeout": 120}

# Shaped like chathub/chathub.yaml: the other app's section is how the chat
# hub sees it (header scheme only); import takes the chathub section and
# resolves the grant against the fooddash app stored in the service.
CHATHUB_YAML = """
apps:
  chathub:
    credentials: {fooddash: env:CHATHUB_FOODDASH_KEY}
  fooddash:
    base_url: http://localhost:8100
    auth: {header: Authorization, prefix: "Bearer "}
    tools:
      ask_fooddash: {type: remote_agent, description: Ask FoodDash's ordering assistant.}
grants:
  - {tool: fooddash.ask_fooddash, to_apps: [chathub]}
agents:
  chathub.assistant:
    instructions: Anything about food goes to FoodDash.
    prompt_version: v1
    tools: [fooddash.ask_fooddash]
"""


def bundle(client, app):
    return client.get(f"/v1/apps/{app}/bundle").json()["bundle"]


def setup_fooddash(client):
    assert client.post("/v1/apps", json={"name": "fooddash", "base_url": "http://fooddash.test:8000",
                                          "auth": AUTH}).status_code == 201
    r = client.post("/v1/apps/fooddash/tools", json=ASK)
    assert r.status_code == 201, r.text
    return r.json()


def test_remote_agent_tool_is_stored_as_authored(ana):
    client, _ = ana
    assert setup_fooddash(client) == ASK
    assert client.get("/v1/apps/fooddash/tools/ask_fooddash").json() == ASK


def test_remote_agent_rejects_fields_the_sdk_sets(ana):
    client, _ = ana
    setup_fooddash(client)
    for extra in ({"method": "GET"}, {"parameters": {"type": "object"}}, {"response_schema": {}}):
        r = client.post("/v1/apps/fooddash/tools", json={**ASK, "name": "ask2", **extra})
        assert r.status_code == 422 and "remote_agent tool can't set" in r.text, extra
    r = client.post("/v1/apps/fooddash/tools", json={**ASK, "name": "ask3", "path": "/agent/chat"})
    assert r.status_code == 422 and "either path or endpoint" in r.text
    r = client.post("/v1/apps/fooddash/tools", json={**ASK, "name": "ask4", "type": "mcp"})
    assert r.status_code == 422


def test_chathub_config_with_the_grant_imports_and_compiles(ana):
    client, _ = ana
    setup_fooddash(client)
    r = client.post("/v1/apps/chathub/import", content=CHATHUB_YAML, headers={"Content-Type": "application/yaml"})
    assert r.status_code == 200, r.text
    b = bundle(client, "chathub")
    granted = b["apps"]["fooddash"]
    assert granted["tools"] == {"ask_fooddash": {k: v for k, v in ASK.items() if k != "name"}}
    assert granted["auth"] == {"header": "Authorization", "prefix": "Bearer "}  # scheme only
    assert "FOODDASH_SERVICE_KEY" not in str(b)
    compiled = load_config(b)
    anvil = compiled.anvil_for("chathub")
    tool = anvil.tools["fooddash.ask_fooddash"]
    assert tool.endpoint == ASK["endpoint"] and tool.method == "POST"
    assert anvil.apps["fooddash"].auth.secret_env == "CHATHUB_FOODDASH_KEY"  # the chat hub's own key
    assert "fooddash.ask_fooddash" in compiled.remote_tools


def test_remote_agent_tool_with_a_path_uses_the_apps_base_url(ana):
    client, _ = ana
    setup_fooddash(client)
    assert client.put("/v1/apps/fooddash/tools/ask_fooddash",
                      json={"type": "remote_agent", "path": "/agent/chat"}).status_code == 200
    client.post("/v1/apps/chathub/import", content=CHATHUB_YAML, headers={"Content-Type": "application/yaml"})
    tool = load_config(bundle(client, "chathub")).anvil_for("chathub").tools["fooddash.ask_fooddash"]
    assert tool.endpoint == "http://fooddash.test:8000/agent/chat"


def test_tools_page_labels_remote_agents(ana):
    client, _ = ana
    setup_fooddash(client)
    page = client.get("/ui/apps/fooddash").text
    assert "remote agent · http://fooddash-agent.test:8100/agent/chat" in page
    assert CALL_CHAIN_HEADER  # the header the SDK sends with these calls
