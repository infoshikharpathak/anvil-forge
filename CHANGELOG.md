# Changelog

## Unreleased

- README: the control-plane quickstart installs plain `garrison-hq` (SQLite,
  the default, needs no database server); Postgres is a separate, optional
  step with `garrison-hq[postgres]` and `DATABASE_URL`. Docs only; no
  package release needed.

## 0.1.0 (2026-11-23)

First release on PyPI, under the Apache-2.0 license, as one family:

| Package | PyPI name | Import |
|---|---|---|
| SDK | `garrison-sdk` | `garrison` |
| Control plane | `garrison-hq` | `garrison_hq` |
| Governance (Anvil) | `garrison-anvil` | `anvil` |
| Orchestration (Agent Forge) | `garrison-forge` | `agent_forge` |

The SDK is `garrison-sdk` because the `garrison` name on PyPI belongs to
another project; its import name is still `garrison`.

### Renamed from Bastion

PyPI doesn't accept the name `bastion` (an old Python 2 standard-library
module), so the project is now Garrison. The GitHub repo moved from
infoshikharpathak/bastion to infoshikharpathak/garrison (GitHub redirects
the old URL).

| Before | Now |
|---|---|
| `pip install bastion`, `import bastion` | `pip install garrison-sdk`, `import garrison` |
| `bastion-server`, `import bastion_server`, CLI `bastion-server` | `garrison-hq`, `import garrison_hq`, CLI `garrison-hq` |
| `BASTION_URL`, `BASTION_KEY`, `BASTION_*` server settings | `GARRISON_URL`, `GARRISON_KEY`, `GARRISON_*` |
| `X-Bastion` (CSRF), `X-Bastion-Call-Chain`, `X-Bastion-Trace-Id` | `X-Garrison`, `X-Garrison-Call-Chain`, `X-Garrison-Trace-Id` |
| app keys `bk_...` | new keys `gk_...` |
| `bastion.a2a`, `bastion.yaml`, `bastion_traces.jsonl` | `garrison.a2a`, `garrison.yaml`, `garrison_traces.jsonl` |

Still accepted, for now (`garrison.compat`):

- the old `BASTION_*` environment variables, when the `GARRISON_*` one isn't
  set, with a one-time deprecation warning per variable;
- the old `X-Bastion*` headers on incoming requests;
- existing `bk_` app keys.

Not carried over: the session cookie is now `garrison_session`, so
dashboard users sign in again once; the SDK's bundle cache moved from
`~/.cache/bastion` to `~/.cache/garrison` and is refilled on the next start.
Database migration revision IDs are unchanged, so an existing database
upgrades in place.

### What's in 0.1.0

- **SDK (`garrison-sdk`, import `garrison`):** apps, tools, agents and flows from one YAML config,
  compiled into the Anvil policy; governed tools and agents; sessions
  across turns; cross-app grants with the consumer's own credentials;
  agent-to-agent calls (`type: remote_agent`, `garrison.a2a.CallerKeys`)
  with caller identity from keys and linked traces; an LLM injection
  classifier on the agents' own provider; fallback replies when an output
  is blocked; combined traces to a local file and the dashboard; remote mode
  (`Platform.from_service`) with an on-disk cache and live bundle refresh.
- **Control plane (`garrison-hq`):** accounts, apps, tools, agents with
  immutable prompt versions, flows, grants, app credentials, app keys, the
  bundle API, YAML import/export, an audit log, trace ingestion, dashboards
  (per app, per trace, per session and across apps), and the CLI (`serve`,
  `migrate`, `create-user`, `cleanup-traces`).
- **Dependencies:** `garrison-anvil` and `garrison-forge[langgraph]` (flows run on
  its graph runner) as normal version ranges (`>=0.1.0,<0.2`); Anthropic models via `pip install "garrison-sdk[anthropic]"`;
  Postgres via `pip install "garrison-hq[postgres]"`.
