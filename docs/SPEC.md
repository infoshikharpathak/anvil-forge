# anvil-forge — Phase 4 spec

New repo: ~/Portfolio Projects/anvil-forge (git init, GitHub repo created by me later)
Layout: sdk/ (package anvil_forge) and server/ (package anvil_forge_server)
First, save this whole spec as docs/SPEC.md and commit it.

THIS SESSION: build 4a only. Stop after 4a and report.
One commit per item, tests for each. Depend on anvil-ai and agent-forge
pinned to their current main commits (git dependencies).

================ 4a: SDK, local mode ================

1. Config model. One YAML (or dict) describing:
   apps (name, base_url, auth env var names), tools (under apps, Phase 2 format),
   agents (qualified "app.agent", instructions, prompt_version, backend, model,
   tools by name), flows (name, a GraphSpec of registered agents), grants.
   Agent tool lists are compiled into the Anvil policy (allowed_tools).
   The Anvil policy is the single source of truth for tool access: Agent Forge
   reads each agent's tools from it, never from a separate list.
   Validation: an agent referencing an unknown tool, or a cross-app tool without
   a grant, fails at load time.

2. AnvilToolSource: implements Agent Forge's ToolSource.
   list_tools() -> the app's Anvil tools plus granted cross-app tools, named
   with Anvil's app-relative spelling (bare / app__tool).
   call(name, args, ctx) -> anvil.execute(agent_id=ctx.agent_name,
   tool_name=resolved, params=args, trace_id=ctx.trace_id).
   Anvil corrective responses are returned to the LLM as the tool result
   (never raised), so agents can self-correct.

3. AnvilHooks: implements Agent Forge's AgentHooks.
   before_agent_run -> check_agent_input, after_agent_run -> check_agent_output,
   on_handoff -> check_handoff. Map Anvil results to HookDecision, keeping the
   correction message.

4. Agent identity. RunContext.agent_name is always the qualified "app.agent"
   for registered agents. Agents spawned by Agent Forge's dynamic planner are
   not supported in platform mode; if one runs, Anvil's unknown_agent hard
   block must stop it (add a test proving this).

5. Classifier. Pass Anvil a classifier that uses the same provider and keys as
   the agents, and record its tokens in the trace so scan cost is visible.

6. Combined trace. One trace per run keyed by trace_id, merging Agent Forge's
   run row and Anvil's call records. Each run records agent, prompt_version,
   model, tokens, cost, latency, tool calls, violations and hook denials.
   Trace exporter interface with sinks:
   - LocalSink (default): full detail including prompts, inputs, outputs,
     tool params. SQLite or JSONL in the app.
   - DashboardSink: interface only in 4a, implemented in 4c. Metadata only by
     default; content is opt-in and redacted.
   Exporting never blocks or fails a run.

7. Public API:
     platform = Platform.from_config("anvil-forge.yaml", app="ironclad")
     reply = await platform.run_agent("support_agent", message, trace_id=None)
     result = await platform.run_flow("dispute_flow", message)
     platform.list_agents(), platform.list_tools()
   The app passes bare names; the SDK qualifies them with its bound app.
   Platform binds Anvil with app=... (Phase 2 caller binding).

8. Tests: end-to-end with a stubbed LLM and a stubbed upstream tool: allowed
   tool call, disallowed tool (corrective response reaches the LLM), sequence
   rule enforced, cross-app denied without grant and allowed with one, input
   blocked by hook, trace_id identical across Forge and Anvil records, local
   trace contains full detail. Plus one live smoke test against OpenAI,
   skipped when no key is set.

9. README with a 20-line quickstart.

================ 4b: Control plane service (later) ================
- Reuse Anvil's registry code (storage, hashed keys, admin session handling,
  policy CRUD) instead of re-implementing auth. Persist sessions (fixes the
  in-memory admin session gap).
- User accounts: email/password with a proper password hash, optional GitHub
  login. A user owns apps.
- Register apps, tools, agents, flows, grants. Prompt versions are immutable;
  an agent points to an active version, and rollback is changing the pointer.
- App keys: shown once, stored hashed, rotatable. Read-only: an app key can
  fetch its bundle and send traces, nothing else.
- GET /bundle (app key): the app's tools, agents, flows, policies, grants and
  active prompt versions, with an ETag. SDK remote mode:
  Platform(api_key=..., url=...) pulls the bundle at startup, caches it to
  disk, re-syncs on an interval via ETag (re-registering tool sources), and
  keeps running on the cached bundle if the service is down.
- The service stores env var NAMES for credentials, never secret values.

================ 4c: Dashboard (later) ================
- POST /traces (app key): write-only, own app only.
- DashboardSink in the SDK: batched, async, local fallback file when the
  service is unreachable, retried later.
- Views: runs across apps and per agent, violations per agent, tokens and cost
  by app / agent / model / prompt version over time, and comparison of two
  prompt versions of one agent. Every row shows trace_id so the app team can
  find full detail in their local trace.
- Content never shown unless the app opted in.
