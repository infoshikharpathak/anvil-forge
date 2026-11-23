"""The README quickstart runs as written: example files, real HTTP to the
bundled fake bank, stubbed model. The README snippets must equal the files."""

import importlib.util
import re
import threading
from pathlib import Path

import pytest
import yaml

from garrison import Platform

from .llm import ScriptedOpenAI

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "quickstart"


@pytest.fixture(autouse=True)
def _clean_registry():
    yield
    from agent_forge.tools import _REGISTRY, sources

    for name in [n for n, fn in _REGISTRY.items() if getattr(fn, "_tool_source", None) is not None]:
        _REGISTRY.pop(name)
    sources._SOURCE_TOOLS.clear()


@pytest.fixture
def fake_bank():
    spec = importlib.util.spec_from_file_location("fake_bank", EXAMPLE / "fake_bank.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    server = mod.serve(port=0)  # any free port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_readme_snippets_match_the_example_files():
    readme = (ROOT / "README.md").read_text()
    quick = readme[readme.index("## Quickstart"):readme.index("## What you get")]
    blocks = dict(re.findall(r"```(yaml|python)\n(.*?)```", quick, re.S))
    assert blocks["yaml"] == (EXAMPLE / "garrison.yaml").read_text()
    assert blocks["python"] == (EXAMPLE / "quickstart.py").read_text()
    assert "python fake_bank.py" in quick  # the "start this first" step


async def test_quickstart_config_runs_against_the_fake_bank(fake_bank):
    cfg = yaml.safe_load((EXAMPLE / "garrison.yaml").read_text())
    assert cfg["apps"]["ironclad"]["tools"]["get_accounts"]["description"] == "List the user's bank accounts."
    cfg["apps"]["ironclad"]["base_url"] = fake_bank
    instructions = cfg["agents"]["ironclad.support_agent"]["instructions"]
    llm = ScriptedOpenAI({instructions: [
        {"tool": "get_accounts", "args": {"user_id": "demo"}},
        {"text": "You have CHK-1001 (checking) and SAV-2002 (savings)."},
    ]})
    p = Platform.from_config(cfg, app="ironclad", openai_client=llm)  # no transport: real HTTP
    reply = await p.run_agent("support_agent", "What accounts do I have?")
    assert reply.status == "success" and "CHK-1001" in reply.content
    (call,) = reply.trace["tool_calls"]
    assert call["success"] is True and call["response"]["accounts"][0]["id"] == "CHK-1001"
