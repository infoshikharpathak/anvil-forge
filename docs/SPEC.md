# garrison — Phase 4 spec

> Renamed from anvil-forge on 2026-11-22, and to Garrison on 2026-11-23
> (packages garrison and garrison-hq; see CHANGELOG.md). The GitHub repo is
> infoshikharpathak/garrison and the folder is ~/Portfolio Projects/garrison.
> The spec below uses the current names.

New repo: ~/Portfolio Projects/anvil-forge (git init, GitHub repo created by me later)
Layout: sdk/ (package garrison) and server/ (package garrison_hq)
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
     platform = Platform.from_config("garrison.yaml", app="ironclad")
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
- Package: server/ is garrison_hq, installed next to the SDK
  (pip install -e sdk -e server). It depends on the SDK and uses the SDK's
  own config compiler for all validation.
- Storage: SQLAlchemy 2 + Alembic migrations, applied at startup and via
  `garrison-hq migrate`. SQLite by default
  (sqlite:///./garrison_hq.db), Postgres via DATABASE_URL
  (postgresql+psycopg://...). CI runs the suite on both, with Postgres as a
  service container. Tables: users, user_sessions, login_attempts, apps,
  app_keys, tools, agents, prompt_versions, flows, grants,
  bundle_revisions, audit_log.
- Ownership: a user owns apps. App names are unique per owner. Every
  management query is scoped to the signed-in user; another user's app is a
  404, never a 403, so names don't leak.
- Accounts: email + password (argon2id). No GitHub login in 4b. Sign-up is
  off by default: the first user comes from `garrison-hq create-user`,
  and GARRISON_SIGNUP=open enables the sign-up page.
- Sessions: a random token in an HttpOnly, SameSite=Strict cookie. The DB
  stores only its SHA-256. 7-day lifetime, logout deletes the row. Every
  write needs the header X-Garrison: 1, which a cross-site form can't send.
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
  with garrison.load_config. Any compile error rejects the write (422 with
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
- App keys: gk_<43 random url-safe chars>, shown once at creation. Stored as
  SHA-256 (high entropy, so no slow hash needed; passwords use argon2). An
  app can have several keys, each with created_at, last_used_at and
  revoked_at.
- SDK API (/v1/sdk, Authorization: Bearer gk_...): GET /v1/sdk/bundle
  returns {app, revision, etag, bundle} with an ETag header, and 304 on
  If-None-Match. App keys reach nothing else (management routes: 401), and
  session cookies don't work on /v1/sdk. Trace upload is 4c (POST
  /v1/traces, the only other route an app key reaches).
- Import/export: POST /v1/apps/{app}/import takes garrison YAML and
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
- Deploy: `garrison-hq serve` (uvicorn), `create-user`, `migrate`;
  server/Dockerfile; a README section.

================ 4c: Dashboard & Trace Viewer ================
(The spec as handed over; "Decisions (4c as built)" at the end of this
section records where the build fills gaps or differs.)

The garrison server (from 4b) already has accounts, apps, the management
API, and the UI for config. This phase adds trace ingestion and a dashboard
to view them.

What exists today:
- The SDK's DashboardSink is defined but has no server endpoint to POST to.
- Traces have two copies by design: a local copy with full detail (the app
  keeps this), and a dashboard copy with metadata only, content opt-in.
- LocalSink writes JSON files to disk. DashboardSink is a stub.
- Each trace already records app, agent, trace_id, session_id,
  bundle_revision, tool calls, timings, and token counts.

Build these, in order:

1. Trace ingestion endpoint: POST /v1/traces (app-key auth, same gk_ keys).
   - Accepts a batch of trace records (list of JSON objects).
   - Validates: app must match the key's app, agent must be registered,
     trace_id is a UUID.
   - Stores in a traces table: id, app_id, agent_id (nullable for
     unregistered), trace_id, session_id, bundle_revision, started_at,
     ended_at, status (success/blocked/error), token_input, token_output,
     model, tool_calls (JSON), steps (JSON, the metadata-only version),
     content (JSON, nullable, only when the app opts in via
     include_content: true in the SDK config).
   - Add an Alembic migration for the table.
   - Response: 202 Accepted with {"accepted": N}.
   - Rate limit: 100 traces per minute per app key (429 with Retry-After).

2. Wire DashboardSink in the SDK (sdk/garrison/trace.py):
   - DashboardSink.__init__(self, url, api_key, *, include_content=False,
     flush_interval=5.0, batch_size=20)
   - Collects traces in a buffer, flushes every flush_interval seconds or
     when batch_size is reached (whichever comes first).
   - POST to {url}/v1/traces with Authorization: Bearer gk_...
   - On 429: back off using Retry-After.
   - On network error or 5xx: buffer locally (up to 500 traces), retry on
     next flush. Drop oldest if the buffer overflows, log a warning.
   - On 401/403: log once, stop retrying (key is bad).
   - include_content controls whether prompt/response text is sent.
     Default false (metadata only).
   - async close() flushes the remaining buffer.
   - Platform.from_service automatically creates a DashboardSink alongside
     the LocalSink.
   - Platform.from_config (library mode) can also add a DashboardSink if
     the YAML has dashboard: { url: ..., key: ..., include_content: false }.

3. Dashboard UI pages (server-side rendered, same pattern as 4b's UI):
   a. Traces list (/ui/apps/{app}/traces): timestamp, agent, session_id
      (truncated), status (color-coded badge), model, tokens (in+out),
      duration_ms. Filterable by agent, status, session_id, date range.
      Paginated, 50 per page, newest first.
   b. Trace detail (/ui/apps/{app}/traces/{trace_id}):
      - Header: trace_id, agent, session_id, bundle_revision (links to the
        prompt version it ran on), model, timestamps, duration, tokens.
      - Steps timeline: each step (agent_start, tool_call, tool_result,
        handoff, agent_end) with timing bars.
      - Tool calls: tool name, app (if cross-app), duration, status
        (allowed/blocked/redacted fields).
      - Content panel (only if content was sent): prompt and responses,
        collapsible.
      - Injection scan results: if a scan fired, the verdict and matched
        pattern.
   c. Session view (/ui/apps/{app}/sessions/{session_id}): all traces in a
      session, in order, showing the multi-turn conversation flow.
   d. Overview dashboard (/ui/apps/{app}/dashboard), the app's landing page:
      - Today's numbers: total traces, success/blocked/error counts, tokens.
      - Traces over time: line chart, last 7 days, bucketed by hour. Inline
        SVG, no JS charting library.
      - Top agents by trace count.
      - Recent blocked traces (last 10).
      - Avg response time by agent.

4. Cross-app tracing: /ui/dashboard (no app scope):
   - All apps the logged-in user owns.
   - Aggregate token usage across apps.
   - Recent traces across all apps.
   - The "tokenomics" view: total tokens by app, by model, by day.

5. Trace cleanup: garrison-hq cleanup-traces --older-than 30d deletes
   traces older than N days. No automatic cleanup (the user decides).

Config: the SDK YAML gains an optional dashboard section
(include_content: false by default). URL and key come from GARRISON_URL and
GARRISON_KEY (same as remote mode); if those are set, the dashboard sink is
active. Library mode without env vars can give explicit url and key.

Tests: ingestion (valid batch, app mismatch, unregistered agent, rate
limit, auth failures); DashboardSink (batching, flush on interval, flush on
size, retry on 5xx, drop on overflow, stop on 401); each dashboard page
renders (200, right template); cross-app dashboard shows only the user's
apps; cleanup deletes old traces and keeps recent ones.

Not included: real-time streaming/WebSockets (polling is fine), alerting,
CSV/JSON export, GitHub login.

After 4c: run garrison-hq serve, import the YAML, create a key, point
the app at it with GARRISON_URL and GARRISON_KEY, and see every agent run,
tool call and token count in the dashboard. Local trace files remain the
full-detail record; the dashboard is the operational view.

Decisions (4c as built):
- Ingestion is per record. Each record in a batch is validated on its
  own; the response is 202 {accepted, duplicates, rejected: [{index,
  trace_id, error}]}, so one bad record doesn't cost the rest. A body that
  isn't a list, or has more than 100 records (GARRISON_TRACE_BATCH_MAX), is
  a 422.
- "agent must be registered" vs "agent_id nullable": every accepted trace
  names a registered agent at ingest time. agent_id is nullable so that
  deleting an agent later keeps its history (ON DELETE SET NULL);
  agent_name is stored too. Agents the platform never registered (Agent
  Forge's planner) can't be a run's top-level agent, so no ingest path for
  them was needed. A cached bundle that still runs an agent the service
  has since deleted will have those traces rejected (logged by the SDK).
- Flow runs: the record has kind "flow", flow (validated) and agent = the
  flow's entry agent, so every trace has an agent.
- Idempotency: a record whose run_id the app already stored is skipped and
  counted as a duplicate. trace_id is not unique (callers may reuse one
  across runs); the detail page lists every run under a trace_id.
- Rate limit: 100 traces/minute per key (GARRISON_TRACE_RATE), counted
  from stored rows (received_at), so it holds across processes. A batch
  that would go over is refused whole with 429 and a Retry-After for when
  it fits. Rejected and duplicate records don't count. Two concurrent
  requests can overshoot slightly; acceptable for a soft limit.
- Extra columns beyond the list: flow_name, app_key_id, run_id,
  prompt_version, duration_ms, cost_usd, unpriced, received_at, and
  summary (JSON: per-agent rows, violations, scans, hook denial count,
  unpriced_models).
- Cost: prices come from Agent Forge's one table (agent_forge.pricing).
  cost_usd is null when no model in the trace has a price, never 0.0, and
  unpriced is true when any part of the trace had none. Dashboard totals
  add up only priced costs, show "unknown" when nothing was priced, and say
  how many traces were left out.
- Status: success; blocked if the run's outcome was blocked or withheld,
  or any hook denied; error if it failed. A run whose tool calls were
  blocked but which answered anyway is success (with violations shown).
- Steps: the SDK now records them with timings (offset_ms from the run's
  start, duration_ms where it applies): agent_start / agent_end from the
  trace hooks, tool_call / tool_result around each Anvil call, handoff from
  on_handoff. An agent that never reached after_agent_run (blocked input,
  exception) gets an agent_end at start + its Agent Forge latency.
- Scans: the LLM classifier now reports its verdict and pattern with its
  usage. Regex scans aren't reported by Anvil, so injection verdicts also
  come from injection_detected_* violations, with the pattern parsed from
  Anvil's correction message.
- Content (opt-in) is redacted in the SDK before it leaves: keys that look
  secret (password, secret, token, api key, authorization, cookie, otp,
  pin, cvv, ssn) at any depth, plus each tool's redact_fields, become
  "[redacted]". Content over 256 KB per record is rejected.
- include_content for remote mode lives on the service: apps.dashboard
  ({include_content}) is part of the bundle, settable by create, PATCH and
  import, and the SDK follows it (also on refresh) unless
  Platform.from_service(include_content=...) is given. The service never
  stores a URL or key: import rejects dashboard.key and drops url and the
  SDK-local flush_interval / batch_size.
- Library mode adds a DashboardSink when the YAML has a dashboard section
  (url/key from it, else GARRISON_URL / GARRISON_KEY), or when both env vars
  are set. dashboard=False or an explicit sinks= turns it off.
- DashboardSink: export() only queues; a background task flushes every
  flush_interval, or early when a full batch is queued (size-triggered
  flushes send only full batches; the timer and close() send everything).
  Order is kept across retries. 422 and other 4xx drop that batch (it can
  never succeed); 401/403 disable the sink. Platform.close() flushes it.
- UI: an app's landing page from the apps list is its dashboard; Config is
  a tab next to Dashboard and Traces. /ui/dashboard is a new nav item with
  a 7/30/90-day window. Counts and sums are SQL; hourly/daily buckets are
  computed in Python from the window's rows, so queries are the same on
  SQLite and Postgres. All times are UTC (Postgres may hand back another
  zone; the server converts).
- The bundle_revision link goes to the prompt version that revision's
  stored bundle names for the agent (anchor on the app's Config page),
  falling back to the label the SDK reported.
- cleanup-traces deletes by started_at across all apps in the database,
  with --dry-run; "30d" and "30" both work.

