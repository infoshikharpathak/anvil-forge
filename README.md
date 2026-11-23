# Garrison

[![garrison-sdk](https://img.shields.io/pypi/v/garrison-sdk.svg)](https://pypi.org/project/garrison-sdk/) [![garrison-hq](https://img.shields.io/pypi/v/garrison-hq.svg)](https://pypi.org/project/garrison-hq/) [![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

One platform on top of [Anvil](https://github.com/infoshikharpathak/anvil) (tool governance) and [Agent Forge](https://github.com/infoshikharpathak/Agent-Forge) (agents). You declare apps, tools, agents and flows once. Every run is then governed by Anvil, executed by Agent Forge, and traced end to end under one `trace_id`.

Status: **4a (SDK), 4b (control plane) and 4c (dashboard and trace viewer) are built.** See [docs/SPEC.md](docs/SPEC.md). Upgrading from the project's earlier name: see [CHANGELOG.md](CHANGELOG.md).

## Install

```bash
pip install garrison-sdk               # the SDK: import garrison
pip install "garrison-sdk[anthropic]"  # + agents on Anthropic models
pip install garrison-hq                # the control plane: the garrison-hq CLI
pip install "garrison-hq[postgres]"    # + the Postgres driver
```

Install names differ from import names: `garrison-sdk` is imported as
`garrison` and `garrison-hq` as `garrison_hq`. Python 3.11 or newer.
`garrison-sdk` brings `garrison-anvil` (Anvil, imported as
`anvil`) and `garrison-forge` (Agent Forge, imported as `agent_forge`) from
PyPI; the install and import names differ for those two.

## Quickstart

Everything is in [`examples/quickstart/`](examples/quickstart/), including a stand-in bank API, so it runs on a clean machine:

```bash
pip install garrison-sdk          # or, from a clone: pip install -e sdk
export OPENAI_API_KEY=...         # agents and the injection classifier use the same key
cd examples/quickstart
python fake_bank.py &             # start this first: a fake bank API on localhost:8765
python quickstart.py
```

`garrison.yaml`:

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
from garrison import Platform

async def main():
    platform = Platform.from_config("garrison.yaml", app="ironclad")
    reply = await platform.run_agent("support_agent", "What accounts do I have?")
    print(reply.content, reply.status, reply.trace_id)

asyncio.run(main())
```

That run is checked by Anvil at every step: the input, each tool call, and the output. It is executed by Agent Forge, and its full trace is appended to `./garrison_traces.jsonl`.

## What you get

- **One config, one source of truth.** Agent tool lists compile into the Anvil policy (`allowed_tools`), and flow edges compile into `allowed_delegations`. Agents read their tools back from that policy. Unknown tools, and cross-app tools or delegations without a grant, fail at load time.
- **Governed tools (`AnvilToolSource`).** The app's tools plus granted cross-app tools are registered with Agent Forge, under app-relative names (`get_accounts`, `notify__send_email`). Every call goes through `anvil.execute`, which enforces policy, sequences and grants, runs injection scans and injects credentials. A blocked call comes back to the model as Anvil's corrective response rather than an exception, so the agent can fix its approach. A model is offered only its policy tools, but any name it calls is still routed through Anvil, so off-policy attempts are corrected and recorded.
- **Governed agents (`AnvilHooks`).** Each agent's input, output and handoff are checked by Anvil's agent checks. A block shows Anvil's correction as the agent's output.
- **Identity.** Registered agents always run as `app.agent`. An agent the platform didn't register (for example, one spawned by Agent Forge's dynamic planner) is stopped by Anvil's `unknown_agent` hard block, even if its name matches a registered short name.
- **Injection classifier.** Anvil gets an LLM classifier on the agents' own provider and keys (hybrid mode: regex first). Its tokens and cost are part of each run's trace.
- **Combined trace.** One record per run merges Agent Forge's run row, Anvil's call records and what the platform saw. It includes, per agent, the prompt version, model, tokens, cost and latency, plus every tool call, violation, hook denial and scan.
  - `LocalSink` (default) keeps full detail: prompts, inputs, outputs and tool params.
  - `DashboardSink` sends a copy to the garrison service's dashboard (see [Dashboard](#dashboard)). Metadata only unless you opt in to content.
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

Instead of a local YAML file, an app can run on what the garrison control plane (`server/`) publishes for it:

```python
platform = Platform.from_service("https://garrison.example.com", api_key=os.environ["GARRISON_KEY"])
```

- **The app comes from the key.** The bundle is fetched at startup and cached on disk (`~/.cache/garrison`, or `cache_dir=`). The cache holds the bundle and its revision, never the key, and the bundle only names env vars.
- **If the service is down at startup**, the platform runs on the cached bundle. With no cache, it fails with an error naming the URL and the cache path. A rejected key always fails.
- **Updates.** After `start()`, the platform polls every `refresh_interval` seconds (default 30, using the ETag, so an unchanged bundle costs a 304). A new revision is compiled and swapped in: runs in progress finish on the config they started with, new runs use the new one, and tools are re-registered. An open `session_id` moves over on its next turn, keeping its conversation and sequence state, so an edited prompt applies mid-chat without a restart. `await platform.refresh()` checks immediately.
- **Traces** record `bundle_revision`, so you can tell which published version a run used, and go to the service's dashboard as well as the local file (`dashboard=False` turns that off).

## Control plane (`server/`)

`garrison-hq` keeps apps, tools, agents, prompt versions, flows and grants in a database, and publishes each app's bundle to the SDK. Every change is compiled with the SDK's own config compiler first, so an invalid change is rejected with the compiler's error and nothing is saved. Every change is versioned and audited.

```bash
pip install garrison-hq                           # or from a clone: pip install -e sdk -e server
garrison-hq create-user --email you@example.com  # the first account (prompts for a password)
garrison-hq serve --port 8200                     # migrates, then serves http://localhost:8200/ui
```

No database server is needed: by default the data lives in a SQLite file,
`./garrison_hq.db` in the folder you run these commands from. For Postgres,
install the driver and point `DATABASE_URL` at it before the commands
above:

```bash
pip install "garrison-hq[postgres]"
export DATABASE_URL=postgresql+psycopg://user:pass@host/db
```

To connect an app:
1. Sign in, then import its `garrison.yaml` (or build it in the UI or API).
2. Create an app key. It's shown once.
3. Run the app with `Platform.from_service("http://localhost:8200", api_key)`.

Prompt edits in the UI reach running apps on their next refresh. Rollback is one click. Every run then shows up in the app's dashboard.

- **Accounts:** argon2 passwords. Sessions live in the database, in an HttpOnly, SameSite=Strict cookie. Every write needs the `X-Garrison: 1` header. Logins are rate-limited per email and per IP. Sign-up is off unless `GARRISON_SIGNUP=open`.
- **App keys:** `gk_…`, stored as SHA-256 and revocable. They can only call `GET /v1/sdk/bundle` and `POST /v1/traces`, for their own app.
- **Docker:** `docker build -f server/Dockerfile -t garrison-hq .`, then `docker run -p 8200:8200 -v garrison-data:/data garrison-hq`. Create the first user with `docker exec -it <container> garrison-hq create-user --email you@example.com`. Behind TLS, set `GARRISON_SECURE_COOKIES=1`.

| Variable | Default | |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./garrison_hq.db` | Postgres: `postgresql+psycopg://…` |
| `GARRISON_SIGNUP` | off | `open` enables the sign-up page |
| `GARRISON_SESSION_DAYS` | `7` | session lifetime |
| `GARRISON_SECURE_COOKIES` | off | `1` behind HTTPS |
| `GARRISON_TRUST_PROXY` | off | `1` to rate-limit by `X-Forwarded-For` behind a proxy |
| `GARRISON_AUTO_MIGRATE` | `1` | `0` to run `garrison-hq migrate` yourself |
| `GARRISON_TRACE_RATE` | `100` | traces accepted per minute per app key |
| `GARRISON_TRACE_BATCH_MAX` | `100` | most traces in one `POST /v1/traces` |

## Dashboard

Apps report every run to the service, and the UI shows them:

- **`/ui/dashboard`**: all your apps. Traces, tokens and estimated cost over 7, 30 or 90 days, tokens by app, by model and by day, and recent traces across apps.
- **`/ui/apps/<app>/dashboard`** (an app's landing page): today's traces by status and tokens, traces per hour over 7 days, agents by trace count with average response time, and recent blocked traces.
- **`/ui/apps/<app>/traces`**: every run, filterable by agent, status, session and date, 50 per page.
- **A trace**: its bundle revision and the prompt version that revision ran, a step timeline (agent start/end, tool call/result, handoff) with timing bars, tool calls with status and redacted fields, injection scan verdicts, violations, and the content if it was sent.
- **A session**: every turn of a `session_id` in order, as a conversation when content was sent.

Remote mode reports automatically. In library mode, add a `dashboard` section, or set `GARRISON_URL` and `GARRISON_KEY`:

```yaml
dashboard:
  include_content: false   # the default: metadata only
  # url: and key: default to GARRISON_URL and GARRISON_KEY; prefer the env vars to a key in the file
```

- **Metadata only by default:** IDs, agents, prompt versions, models, tokens, cost, timings, tool names and statuses, violation types and scan verdicts. With `include_content: true`, prompts, inputs, outputs and tool params and responses are sent too, with secret-looking fields (password, token, key, otp, pin, ...) and each tool's `redact_fields` replaced by `[redacted]`. For a remote-mode app, set it on the service (`PATCH /v1/apps/<app>` with `{"dashboard": {"include_content": true}}`, or in the imported YAML); `Platform.from_service(..., include_content=False)` overrides it in code.
- **Delivery:** `DashboardSink` queues each trace and posts batches of 20 every 5 seconds in the background. If the service is down, it keeps up to 500 traces and retries; past that it drops the oldest, with a warning. On 429 it waits for `Retry-After`. A rejected key (401/403) stops it, once, with an error. `await platform.close()` sends what's left.
- **What the service checks:** the record's app must be the key's app, the agent (and flow) must be registered, and `trace_id` must be a UUID. The SDK's default trace IDs are UUIDs; a `trace_id=` you pass yourself must be one too, or the service rejects that trace (the local copy is unaffected). Records with a `run_id` already stored are skipped, so a retried batch isn't stored twice. Each key may send 100 traces a minute.
- **Retention:** nothing is deleted automatically. `garrison-hq cleanup-traces --older-than 30d` deletes traces that started more than 30 days ago (`--dry-run` counts them first).
- The local trace file is still the full record. The dashboard is the operational view.

## Config reference

| Section | Shape |
|---|---|
| `apps` | Anvil Phase 2 apps: `base_url`, `auth` (header + `secret_env` env var name, never the secret), `tools` (`path`, `method`, `trust`, `parameters`, `redact_fields`, ...). `redact_fields` strips secrets a tool returns before the agent or any trace sees them. |
| `agents` | `app.agent`: `instructions`, `prompt_version`, `backend` (`openai` \| `anthropic`), `model`, `tools` (bare = own app, `app.tool` = granted), optional `description`, `sequences`, `strictness`, `trust_overrides` |
| `flows` | `app.flow`: `entry`, `nodes` (`agent`, `task`), `edges` (`from`, `to`, optional `condition_key`). Nodes must be the flow's own app's agents. |
| `grants` | `{tool: notify.send_email, to_apps: [ironclad]}`. A granted tool is called with the **consumer's own credential**, issued to it by the granting app: the consumer app lists `credentials: {notify: env:IRONCLAD_NOTIFY_KEY}`. The granting app's secret is never read by the consumer (its `auth` there may be just `header` and `prefix`), and a granted tool from an app with `auth` but no consumer credential is a load-time `ConfigError`. Control-plane bundles carry only the granting app's header scheme. |
| `settings` | Passed to Anvil (for example `injection_scanning`) |
| `classifier` | `enabled` (default true), `provider`, `model` |
| `dashboard` | optional: `include_content` (default false), `url`, `key` (default to `GARRISON_URL` / `GARRISON_KEY`), `flush_interval` (5), `batch_size` (20) |

## Agent-to-agent (A2A)

An agent can call another app's agent as a tool. In the caller's config the
other app's section declares it with `type: remote_agent`:

```yaml
apps:
  fooddash:
    base_url: http://localhost:8100
    auth: {header: Authorization, prefix: "Bearer "}      # scheme only
    tools:
      ask_fooddash: {type: remote_agent, description: "Ask FoodDash's ordering agent."}
  chathub:
    credentials: {fooddash: env:CHATHUB_FOODDASH_KEY}       # issued by FoodDash
grants: [{tool: fooddash.ask_fooddash, to_apps: [chathub]}]
```

It POSTs `{message, session_id}` to `{base_url}/agent/chat` (the model only
writes `message`; the remote session is one per caller session) with the
caller's own credential, `X-Garrison-Call-Chain` (e.g. `chathub.assistant`)
and `X-Garrison-Trace-Id`. It's untrusted by default, so the other agent's
reply is injection-scanned like any tool response, and the caller's trace
lists the callee's trace in `child_traces`.

The receiving app authenticates the caller with `garrison.a2a.CallerKeys`
(keys it issued, stored as SHA-256) and starts the run with
`run_agent(..., caller_app=caller.app, parent_trace_id=caller.parent_trace_id,
chain=caller.chain)`. **Identity comes from the key only**: a header that
claims another app changes nothing. Traces carry `caller_app`,
`parent_trace_id` and `chain`, and the dashboard links a trace to its parent
and its children across apps.

**Limit: only the direct caller is verified.** The chain header and the
parent trace ID are whatever the direct caller sent; hops before it are
unverified. Signed call chains (each hop signs what it forwards, with keys
from the control plane) are future work.

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
python3.12 -m venv .venv && .venv/bin/pip install -e "sdk[dev]" -e "server[dev,postgres]"
.venv/bin/pytest -q sdk        # stubbed LLM + upstream; the live OpenAI test runs only if OPENAI_API_KEY is real
```

## License

Apache License 2.0. See [LICENSE](LICENSE).
