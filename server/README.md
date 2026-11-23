# garrison-hq

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

## From source

```bash
pip install -e sdk -e "server[dev]"     # from the repo root
pytest -q server                        # add TEST_POSTGRES_URL=... to also run on Postgres
```

## License

Apache License 2.0. See [LICENSE](https://github.com/infoshikharpathak/garrison/blob/main/LICENSE).
