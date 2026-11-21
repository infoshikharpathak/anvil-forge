import copy
import os

import pytest

# Dummy keys let SDK clients be constructed; stubbed tests never call out.
# A real OPENAI_API_KEY in the environment enables the live smoke test.
os.environ.setdefault("OPENAI_API_KEY", "dummy-key-for-tests")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-key-for-tests")

BASE = {
    "apps": {
        "ironclad": {
            "base_url": "http://bank.test",
            "auth": {"header": "Authorization", "secret_env": "IRONCLAD_TOKEN", "prefix": "Bearer "},
            "tools": {
                "get_accounts": {
                    "path": "/accounts", "method": "GET", "trust": "trusted",
                    "description": "List the user's accounts.",
                    "parameters": {"type": "object", "properties": {"user_id": {"type": "string"}}, "required": ["user_id"]},
                },
                "initiate_transfer": {
                    "path": "/transfers", "method": "POST", "trust": "untrusted",
                    "description": "Start a transfer.",
                    "parameters": {
                        "type": "object",
                        "properties": {"user_id": {"type": "string"}, "amount": {"type": "number"}},
                        "required": ["user_id", "amount"],
                    },
                },
                "close_account": {"path": "/close", "method": "POST", "trust": "trusted", "description": "Close an account."},
            },
        },
        "notify": {
            "base_url": "http://notify.test",
            "tools": {
                "send_email": {
                    "path": "/email", "method": "POST", "trust": "trusted", "description": "Email a customer.",
                    "parameters": {"type": "object", "properties": {"to": {"type": "string"}, "body": {"type": "string"}}, "required": ["to", "body"]},
                },
                "send_sms": {"path": "/sms", "method": "POST", "trust": "trusted", "description": "Text a customer."},
            },
        },
    },
    "agents": {
        "ironclad.support_agent": {
            "instructions": "You are Ironclad's support agent.",
            "prompt_version": "v3",
            "backend": "openai",
            "model": "gpt-test",
            "tools": ["get_accounts", "initiate_transfer"],
            "sequences": [{"before": "initiate_transfer", "requires": ["get_accounts"], "match_fields": ["user_id"]}],
        },
        "ironclad.triage": {
            "instructions": "You triage disputes.", "prompt_version": "v1", "model": "gpt-test",
            "tools": [],
        },
        "ironclad.resolver": {
            "instructions": "You resolve disputes and email the customer.", "prompt_version": "v2",
            "backend": "anthropic", "model": "claude-test",
            "tools": ["notify.send_email"],
        },
        "notify.dispatcher": {
            "instructions": "You send notifications.", "prompt_version": "v1",
            "tools": ["send_email", "send_sms"],
        },
    },
    "flows": {
        "ironclad.dispute_flow": {
            "entry": "triage",
            "nodes": [{"agent": "triage", "task": "Classify the dispute."}, {"agent": "resolver", "task": "Resolve it."}],
            "edges": [{"from": "triage", "to": "resolver"}],
        },
    },
    "grants": [{"tool": "notify.send_email", "to_apps": ["ironclad"]}],
    "classifier": {"enabled": False},
}


def base_config() -> dict:
    return copy.deepcopy(BASE)


@pytest.fixture(autouse=True)
def _isolated_dbs(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_FORGE_DB_PATH", str(tmp_path / "forge.db"))
    monkeypatch.chdir(tmp_path)  # Anvil's trace fallback dir and local traces land here
