# garrison-hq

[![PyPI](https://img.shields.io/pypi/v/garrison-hq.svg)](https://pypi.org/project/garrison-hq/) [![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://github.com/infoshikharpathak/garrison/blob/main/LICENSE)

The [Garrison](https://github.com/infoshikharpathak/garrison) control plane
(install `garrison-hq`, import `garrison_hq`):

- accounts, with database sessions, CSRF protection and a login rate limit;
- apps, tools, agents with immutable prompt versions, flows and grants, each
  change compiled by the SDK's config compiler before it's saved, versioned
  and audited;
- app keys, and the bundle API that the SDK's remote mode
  (`Platform.from_service`) pulls from;
- trace ingestion and dashboards: per-app traces, sessions, tokens and cost.

## Install and run

```bash
pip install garrison-hq                 # SQLite by default
pip install "garrison-hq[postgres]"     # + the Postgres driver (psycopg)

garrison-hq create-user --email you@example.com   # the first account (prompts for a password)
garrison-hq serve --port 8200                     # migrates, then serves http://localhost:8200/ui
garrison-hq migrate                               # run migrations yourself (with GARRISON_AUTO_MIGRATE=0)
garrison-hq cleanup-traces --older-than 30d       # delete old traces (--dry-run counts them)
```

`DATABASE_URL` picks the database (default `sqlite:///./garrison_hq.db`, or
`postgresql+psycopg://user:pass@host/db`). The other settings are
`GARRISON_*` environment variables, listed in the
[repo README](https://github.com/infoshikharpathak/garrison#control-plane-server).
Python 3.11 or newer.

## Trace content is off by default

Traces arrive with metadata only: IDs, agents, prompt versions, models,
tokens, cost, timings, tool names and statuses, violation types and scan
verdicts. Content (the agent's input and output, prompts, tool params and
responses, and Anvil's corrective messages) is not sent unless the app opts
in. Secret-looking fields and each tool's `redact_fields` are replaced with
`[redacted]` before content leaves the app.

To turn it on for one app that runs in remote mode (`Platform.from_service`):

```bash
# with a signed-in session cookie; every write needs the X-Garrison header
curl -X PATCH http://localhost:8200/v1/apps/<app> -H 'X-Garrison: 1' \
     -H 'Content-Type: application/json' -d '{"dashboard": {"include_content": true}}'
```

or put it in the app's YAML and import it:

```yaml
dashboard:
  include_content: true
```

The app picks it up on its next bundle refresh (default every 30 seconds).
`Platform.from_service(..., include_content=False)` overrides it in code. An
app in library mode sets `dashboard: {include_content: true}` in its own
config instead; the service's setting doesn't apply to it.

## Remote agents as tools

A tool with `type: remote_agent` is another app's agent: the SDK POSTs
`{message, session_id}` to it and the model only writes the message. Give it
a full `endpoint` when the agent runs on its own host, or a `path` (default
`/agent/chat`) on the app's `base_url`. The SDK sets its method and
parameters, so those fields are rejected. Grant it to another app like any
tool; the consumer calls it with its own key (`credentials`), so the
granting app needs an `auth` header scheme.

```bash
curl -X POST http://localhost:8200/v1/apps/fooddash/tools -H 'X-Garrison: 1' \
     -H 'Content-Type: application/json' \
     -d '{"name": "ask_fooddash", "type": "remote_agent", "endpoint": "http://localhost:8100/agent/chat"}'
```

## From source

```bash
pip install -e sdk -e "server[dev]"     # from the repo root
pytest -q server                        # add TEST_POSTGRES_URL=... to also run on Postgres
```

## License

Apache License 2.0. See [LICENSE](https://github.com/infoshikharpathak/garrison/blob/main/LICENSE).
