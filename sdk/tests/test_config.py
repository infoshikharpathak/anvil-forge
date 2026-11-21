"""Item 1: config model compiled into a single Anvil policy."""

import pytest
import yaml

from anvil_forge.config import POLICY_NAME, ConfigError, load_config

from .conftest import base_config


def test_compiles_tools_and_flow_delegations_into_anvil_policy():
    cfg = load_config(base_config())
    agents = cfg.anvil.policies[POLICY_NAME].agents
    assert set(agents) == {"ironclad.support_agent", "ironclad.triage", "ironclad.resolver", "notify.dispatcher"}
    assert agents["ironclad.support_agent"].allowed_tools == ["get_accounts", "initiate_transfer"]
    assert agents["ironclad.triage"].allowed_delegations == ["resolver"]  # from the flow edge
    assert agents["ironclad.support_agent"].sequences[0].before == "initiate_transfer"
    assert "ironclad.get_accounts" in cfg.anvil.tools
    assert cfg.anvil.tools["ironclad.get_accounts"].endpoint == "http://bank.test/accounts"


def test_agent_tools_come_from_the_anvil_policy():
    cfg = load_config(base_config())
    assert cfg.agent_tools("ironclad.resolver") == ["notify__send_email"]
    assert cfg.agent_tools("ironclad.support_agent") == ["get_accounts", "initiate_transfer"]
    # the policy is the source of truth: changing it changes what the agent gets,
    # changing the original platform list does not
    cfg.platform.agents["ironclad.support_agent"].tools.append("close_account")
    assert "close_account" not in cfg.agent_tools("ironclad.support_agent")
    cfg.anvil.policies[POLICY_NAME].agents["ironclad.support_agent"].allowed_tools.append("close_account")
    assert "close_account" in cfg.agent_tools("ironclad.support_agent")


def test_yaml_file(tmp_path):
    path = tmp_path / "anvil-forge.yaml"
    path.write_text(yaml.safe_dump(base_config()))
    assert "ironclad.dispute_flow" in load_config(path).platform.flows


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda c: c["agents"]["ironclad.support_agent"]["tools"].append("teleport"), "unregistered tool 'ironclad.teleport'"),
        (lambda c: c["agents"]["ironclad.support_agent"]["tools"].append("notify.send_sms"), "without a grant"),
        (lambda c: c.update(grants=[]), "'notify.send_email' from app 'notify' without a grant"),
        (lambda c: c["agents"].update({"bare_agent": {"instructions": "x", "prompt_version": "v1"}}), "must be qualified"),
        (lambda c: c["agents"].update({"ghost.a": {"instructions": "x", "prompt_version": "v1"}}), "undeclared app 'ghost'"),
        (lambda c: c["agents"]["ironclad.triage"].pop("prompt_version"), "prompt_version"),
        (lambda c: c["agents"]["ironclad.triage"].update(backend="gemini"), "backend"),
        (lambda c: c["flows"]["ironclad.dispute_flow"]["nodes"].append({"agent": "nobody"}), "unregistered agent 'ironclad.nobody'"),
        (lambda c: c["flows"]["ironclad.dispute_flow"].update(entry="resolverx"), "entry 'resolverx'"),
        (lambda c: c["flows"]["ironclad.dispute_flow"]["edges"].append({"from": "triage", "to": "nobody"}), "node not in the flow"),
        (lambda c: c["flows"]["ironclad.dispute_flow"]["nodes"].append({"agent": "notify.dispatcher"}), "flow nodes must be agents of app 'ironclad'"),
        (lambda c: c.update(extra_section={}), "extra_section"),
    ],
)
def test_invalid_configs_fail_at_load(mutate, message):
    cfg = base_config()
    mutate(cfg)
    with pytest.raises(ConfigError, match=message):
        load_config(cfg)
