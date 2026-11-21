# bastion — Phase 4 spec

> Renamed from anvil-forge to bastion on 2026-11-22 (packages bastion and
> bastion-server). The GitHub repo is infoshikharpathak/bastion and the
> folder is ~/Portfolio Projects/bastion. The original spec below keeps
> the old names.

New repo: ~/Portfolio Projects/anvil-forge (git init, GitHub repo created by me later)
Layout: sdk/ (package bastion) and server/ (package bastion_server)
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
     platform = Platform.from_config("bastion.yaml", app="ironclad")
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

================ 4b: Control plane service ================
(Original bullets kept below; "Decisions" supersedes them where they differ.)

Original:
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

Decisions (4b as built):
- Correction: Anvil's registry/ is not part of the installed anvil package,
  so the server does not import it and does not need it running. It copies
  the registry's patterns: SHA-256 hashed keys, DB-backed cookie sessions,
  and a required CSRF header on writes (like Anvil's X-Anvil-UI).
- Package: server/ is bastion_server, installed next to the SDK
  (pip install -e sdk -e server). It depends on the SDK and uses the SDK's
  own config compiler for all validation.
- Storage: SQLAlchemy 2 + Alembic migrations, applied at startup and via
  `bastion-server migrate`. SQLite by default
  (sqlite:///./bastion_server.db), Postgres via DATABASE_URL
  (postgresql+psycopg://...). CI runs the suite on both, with Postgres as a
  service container. Tables: users, user_sessions, login_attempts, apps,
  app_keys, tools, agents, prompt_versions, flows, grants,
  bundle_revisions, audit_log.
- Ownership: a user owns apps. App names are unique per owner. Every
  management query is scoped to the signed-in user; another user's app is a
  404, never a 403, so names don't leak.
- Accounts: email + password (argon2id). No GitHub login in 4b. Sign-up is
  off by default: the first user comes from `bastion-server create-user`,
  and BASTION_SIGNUP=open enables the sign-up page.
- Sessions: a random token in an HttpOnly, SameSite=Strict cookie. The DB
  stores only its SHA-256. 7-day lifetime, logout deletes the row. Every
  write needs the header X-Bastion: 1, which a cross-site form can't send.
- Login rate limit: 5 failed attempts per email, or 20 per client IP, in 15
  minutes, then 429 until the window passes. Attempts are stored in the DB,
  so the limit holds across processes and restarts.
- Credentials: tools and apps store auth as {header, secret_env, prefix}.
  The schema has no field that can hold a secret value, and unknown fields
  are rejected.
- Prompt versions: immutable rows (version 1, 2, ...; label "v<n>", or the
  imported label). Changing an agent's instructions creates the next version
  and points the agent at it. Rollback only moves the pointer.
- Validation: every write builds the bundle of the app it touches, plus the
  bundle of every app that depends on it through grants, and compiles each
  with bastion.load_config. Any compile error rejects the write (422 with
  the compiler's message) and nothing is saved.
- Bundles: an app's bundle is the SDK config format exactly: the app itself
  (base_url, auth, tools), its agents (instructions and prompt_version from
  the active version), flows, grants that give it other apps' tools, and,
  for each such granting app, its base_url, auth and only the granted tools.
  Each successful write that changes an app's bundle stores a new
  bundle_revision (monotonic per app) with an ETag (SHA-256 of the canonical
  JSON). A write that leaves a bundle byte-identical creates no revision.
- Audit: every successful write adds an audit_log row: user, app, action,
  entity type and name, before/after summaries, and time.
- App keys: bk_<43 random url-safe chars>, shown once at creation. Stored as
  SHA-256 (high entropy, so no slow hash needed; passwords use argon2). An
  app can have several keys, each with created_at, last_used_at and
  revoked_at.
- SDK API (/v1/sdk, Authorization: Bearer bk_...): GET /v1/sdk/bundle
  returns {app, revision, etag, bundle} with an ETag header, and 304 on
  If-None-Match. App keys reach nothing else (management routes: 401), and
  session cookies don't work on /v1/sdk. Trace upload is 4c.
- Import/export: POST /v1/apps/{app}/import takes bastion YAML and
  replaces that app's tools, agents, flows, grants-to-it, settings and
  classifier. Other apps the YAML mentions must already exist under the
  same user (or are only grant sources). GET /v1/apps/{app}/export returns
  the bundle as YAML.
- SDK remote mode: Platform.from_service(url, api_key, cache_dir=...,
  refresh_interval=...). The app comes from the key.
  - Startup: fetches the bundle synchronously and caches it
    (<cache_dir>/<sha256(key)[:16]>.json, holding the bundle, revision and
    ETag, never the key). If the service is down it uses the cache; with no
    cache it raises PlatformError naming the URL and cache path.
  - Refresh: a background task polls with If-None-Match. On a new revision
    it compiles the bundle and swaps config, policy and tool registrations
    atomically. Runs in progress finish on the config they started with.
  - Sessions: an open session moves to the new config on its next turn. It
    keeps Anvil's sequence state (records are copied) and the conversation
    history, so an edited prompt applies to the next message without a
    restart.
  - Tracing: each run's trace records bundle_revision.
- UI: server-rendered (Jinja2) like Anvil's registry, under /ui. Pages: sign
  up, log in, apps list, app detail (tools, agents with prompt history and
  rollback, flows, grants, keys with create/revoke), and the audit log.
  Writes go through the same /v1 API with the CSRF header. A nav slot is
  reserved for 4c's trace views.
- Deploy: `bastion-server serve` (uvicorn), `create-user`, `migrate`;
  server/Dockerfile; a README section.

================ 4c: Dashboard (later) ================
- POST /traces (app key): write-only, own app only.
- DashboardSink in the SDK: batched, async, local fallback file when the
  service is unreachable, retried later.
- Views: runs across apps and per agent, violations per agent, tokens and cost
  by app / agent / model / prompt version over time, and comparison of two
  prompt versions of one agent. Every row shows trace_id so the app team can
  find full detail in their local trace.
- Content never shown unless the app opted in.
