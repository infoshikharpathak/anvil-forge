"""Cross-app tools are called with the consumer's own credential, issued to
it by the granting app. The granting app's secret never leaves it."""

import pytest

from garrison import Platform
from garrison.config import ConfigError, load_config

from .llm import ScriptedOpenAI
from .upstream import Upstream

BANK_SECRET, HUB_SECRET = "bank-own-secret", "hub-partner-key"


def hub_config(credentials=True, bank_auth=None):
    hub = {"tools": {}}
    if credentials:
        hub["credentials"] = {"ironclad": "env:HUB_IRONCLAD_KEY"}
    return {
        "apps": {
            "ironclad": {
                "base_url": "http://bank.test",
                "auth": bank_auth or {"header": "Authorization", "secret_env": "IRONCLAD_TOKEN", "prefix": "Bearer "},
                "tools": {"search": {"path": "/policies/search", "method": "GET", "trust": "trusted",
                                     "description": "Search policies.",
                                     "parameters": {"type": "object", "properties": {"q": {"type": "string"}},
                                                    "required": ["q"]}}},
            },
            "chathub": hub,
        },
        "agents": {"chathub.assistant": {"instructions": "You help.", "prompt_version": "v1", "model": "gpt-test",
                                         "tools": ["ironclad.search"]},
                   "ironclad.banker": {"instructions": "You bank.", "prompt_version": "v1", "model": "gpt-test",
                                       "tools": ["search"]}},
        "grants": [{"tool": "ironclad.search", "to_apps": ["chathub"]}],
        "classifier": {"enabled": False},
    }


@pytest.fixture(autouse=True)
def _clean_registry():
    yield
    from agent_forge.tools import _REGISTRY, sources

    for name in [n for n, fn in _REGISTRY.items() if getattr(fn, "_tool_source", None) is not None]:
        _REGISTRY.pop(name)
    sources._SOURCE_TOOLS.clear()


@pytest.fixture(autouse=True)
def _secrets(monkeypatch):
    monkeypatch.setenv("IRONCLAD_TOKEN", BANK_SECRET)
    monkeypatch.setenv("HUB_IRONCLAD_KEY", HUB_SECRET)


async def call_search(config, app, agent, tool_name):
    upstream = Upstream()
    instructions = config["agents"][f"{app}.{agent}"]["instructions"]
    llm = ScriptedOpenAI({instructions: [{"tool": tool_name, "args": {"q": "limits"}}, {"text": "ok"}]})
    p = Platform.from_config(config, app=app, openai_client=llm, http_transport=upstream.transport(), sinks=[])
    await p.run_agent(agent, "limits?")
    return upstream


async def test_consumer_sends_its_own_credential():
    upstream = await call_search(hub_config(), "chathub", "assistant", "ironclad__search")
    (req,) = upstream.requests
    assert req.headers["authorization"] == f"Bearer {HUB_SECRET}"
    assert BANK_SECRET not in str(req.headers) and BANK_SECRET not in str(req.url)


async def test_granting_app_still_uses_its_own_key_for_its_own_agents():
    upstream = await call_search(hub_config(), "ironclad", "banker", "search")
    (req,) = upstream.requests
    assert req.headers["authorization"] == f"Bearer {BANK_SECRET}"


def test_the_consumer_config_never_holds_the_granting_secret_env():
    compiled = load_config(hub_config())
    assert compiled.anvil_for("chathub").apps["ironclad"].auth.secret_env == "HUB_IRONCLAD_KEY"
    assert compiled.anvil_for("ironclad").apps["ironclad"].auth.secret_env == "IRONCLAD_TOKEN"


async def test_scheme_only_auth_on_the_granting_app_is_enough():
    cfg = hub_config(bank_auth={"header": "Authorization", "prefix": "Bearer "})
    cfg["agents"].pop("ironclad.banker")
    upstream = await call_search(cfg, "chathub", "assistant", "ironclad__search")
    assert upstream.requests[0].headers["authorization"] == f"Bearer {HUB_SECRET}"


def test_missing_consumer_credential_fails_at_load():
    with pytest.raises(ConfigError, match="no credential for app 'ironclad'"):
        load_config(hub_config(credentials=False))


@pytest.mark.parametrize("value", ["HUB_IRONCLAD_KEY", "env:", 42])
def test_credential_must_be_env_reference(value):
    cfg = hub_config()
    cfg["apps"]["chathub"]["credentials"] = {"ironclad": value}
    with pytest.raises(ConfigError, match="must be 'env:VAR'"):
        load_config(cfg)
