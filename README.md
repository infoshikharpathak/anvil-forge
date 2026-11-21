# anvil-forge

One platform on top of [Anvil](https://github.com/infoshikharpathak/anvil) (tool governance) and [Agent Forge](https://github.com/infoshikharpathak/Agent-Forge) (agents). You declare apps, tools, agents and flows once. Every run is then governed by Anvil, executed by Agent Forge, and traced end to end under one `trace_id`.

Status: **Phase 4a (SDK, local mode)**. The control plane (4b) and dashboard (4c) are next. See [docs/SPEC.md](docs/SPEC.md).

## Quickstart

Everything is in [`examples/quickstart/`](examples/quickstart/), including a stand-in bank API, so it runs on a clean machine:

```bash
pip install -e sdk                # pulls anvil-ai and agent-forge from GitHub (pinned commits)
export OPENAI_API_KEY=...         # agents and the injection classifier use the same key
cd examples/quickstart
python fake_bank.py &             # start this first: a fake bank API on localhost:8765
python quickstart.py
```

`anvil-forge.yaml`:

```yaml
apps:
  ironclad:
    base_url: http://localhost:8765
    tools:
      get_accounts: {path: /accounts, method: GET, trust: trusted, description: List the user's bank accounts.,
                     parameters: {type: object, properties: {user_id: {type: string}}, required: [user_id]}}
agents:
  ironclad.support_agent:
    instructions: You are Ironclad's support agent for user_id "demo". Look up accounts before answering.
    prompt_version: v1
    tools: [get_accounts]
```

`quickstart.py`:

```python
import asyncio
from anvil_forge import Platform

async def main():
    platform = Platform.from_config("anvil-forge.yaml", app="ironclad")
    reply = await platform.run_agent("support_agent", "What accounts do I have?")
    print(reply.content, reply.status, reply.trace_id)

asyncio.run(main())
```

That run is checked by Anvil at every step: the input, each tool call, and the output. It is executed by Agent Forge, and its full trace is appended to `./anvil_forge_traces.jsonl`.

## What you get

- **One config, one source of truth.** Agent tool lists compile into the Anvil policy (`allowed_tools`), and flow edges compile into `allowed_delegations`. Agents read their tools back from that policy. Unknown tools, and cross-app tools or delegations without a grant, fail at load time.
- **Governed tools (`AnvilToolSource`).** The app's tools plus granted cross-app tools are registered with Agent Forge, under app-relative names (`get_accounts`, `notify__send_email`). Every call goes through `anvil.execute`, which enforces policy, sequences and grants, runs injection scans and injects credentials. A blocked call comes back to the model as Anvil's corrective response rather than an exception, so the agent can fix its approach. A model is offered only its policy tools, but any name it calls is still routed through Anvil, so off-policy attempts are corrected and recorded.
- **Governed agents (`AnvilHooks`).** Each agent's input, output and handoff are checked by Anvil's agent checks. A block shows Anvil's correction as the agent's output.
- **Identity.** Registered agents always run as `app.agent`. An agent the platform didn't register (for example, one spawned by Agent Forge's dynamic planner) is stopped by Anvil's `unknown_agent` hard block, even if its name matches a registered short name.
- **Injection classifier.** Anvil gets an LLM classifier on the agents' own provider and keys (hybrid mode: regex first). Its tokens and cost are part of each run's trace.
- **Combined trace.** One record per run merges Agent Forge's run row, Anvil's call records and what the platform saw. It includes, per agent, the prompt version, model, tokens, cost and latency, plus every tool call, violation, hook denial and scan.
  - `LocalSink` (default) keeps full detail: prompts, inputs, outputs and tool params.
  - `DashboardSink` is an interface for 4c: metadata only by default.
  - Exporting never blocks or fails a run.

```python
await platform.run_agent("support_agent", message, session_id="chat-42")  # multi-turn
await platform.run_flow("dispute_flow", message)   # a graph of registered agents
platform.list_agents(); platform.list_tools(); platform.list_flows()
Platform.from_config(cfg, app="ironclad", sinks=[LocalSink("traces/run.jsonl")])
Platform.from_config(cfg, app="ironclad",           # fill in values the model must not choose
                     prepare_tool_call=lambda ctx, tool, params: {**params, "user_id": current_user()})
platform.session_records("chat-42")                  # the session's Anvil records (tool calls, checks)
platform.session_prerequisites("chat-42", "support_agent")  # sequence prerequisites already satisfied
platform.describe_agent("support_agent")             # effective policy: tools + trust, sequences, settings
```

## Remote mode

Instead of a local YAML file, an app can run on what the anvil-forge control plane (`server/`) publishes for it:

```python
platform = Platform.from_service("https://forge.example.com", api_key=os.environ["ANVIL_FORGE_KEY"])
```

- **The app comes from the key.** The bundle is fetched at startup and cached on disk (`~/.cache/anvil-forge`, or `cache_dir=`). The cache holds the bundle and its revision, never the key, and the bundle only names env vars.
- **If the service is down at startup**, the platform runs on the cached bundle. With no cache, it fails with an error naming the URL and the cache path. A rejected key always fails.
- **Updates.** After `start()`, the platform polls every `refresh_interval` seconds (default 30, using the ETag, so an unchanged bundle costs a 304). A new revision is compiled and swapped in: runs in progress finish on the config they started with, new runs use the new one, and tools are re-registered. An open `session_id` moves over on its next turn, keeping its conversation and sequence state, so an edited prompt applies mid-chat without a restart. `await platform.refresh()` checks immediately.
- **Traces** record `bundle_revision`, so you can tell which published version a run used.



| Section | Shape |
|---|---|
| `apps` | Anvil Phase 2 apps: `base_url`, `auth` (header + `secret_env` env var name, never the secret), `tools` (`path`, `method`, `trust`, `parameters`, `redact_fields`, ...). `redact_fields` strips secrets a tool returns before the agent or any trace sees them. |
| `agents` | `app.agent`: `instructions`, `prompt_version`, `backend` (`openai` \| `anthropic`), `model`, `tools` (bare = own app, `app.tool` = granted), optional `description`, `sequences`, `strictness`, `trust_overrides` |
| `flows` | `app.flow`: `entry`, `nodes` (`agent`, `task`), `edges` (`from`, `to`, optional `condition_key`). Nodes must be the flow's own app's agents. |
| `grants` | `{tool: notify.send_email, to_apps: [ironclad]}` |
| `settings` | Passed to Anvil (for example `injection_scanning`) |
| `classifier` | `enabled` (default true), `provider`, `model` |

## Things to know

- **Sessions.** Pass `session_id=` to `run_agent` or `run_flow` to carry a conversation across turns. Without one, every run starts clean.
  - **Shared Anvil state:** runs in one session share one Anvil session, so sequence rules span messages (verify in one turn, transfer in the next).
  - **Agent memory:** `run_agent` reuses the agent, so it remembers the conversation. Flow agents start fresh each turn.
  - **Ordering:** turns in the same session are serialized.
  - **Expiry:** sessions expire after Anvil's `session_ttl` without use, and `end_session(id)` ends one early.
  - **Traces:** every turn still gets its own trace, tagged with `session_id`.
- Tool names live in Agent Forge's process-wide registry. Run one `Platform` per app per process. A tool can't reuse a built-in Agent Forge tool name (such as `web_search`).
- Agent Forge's graph runner retries a node whose output is under 50 characters.
- Backends: OpenAI and Anthropic. The Anthropic path is tested with stubs only; the live smoke test uses OpenAI.

## Development

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e "sdk[dev]"
.venv/bin/pytest -q sdk        # stubbed LLM + upstream; the live OpenAI test runs only if OPENAI_API_KEY is real
```
