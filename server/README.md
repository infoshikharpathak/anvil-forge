# bastion-server

The bastion control plane:
- accounts;
- apps, tools, agents with immutable prompt versions, flows and grants;
- app keys;
- the bundle API that the SDK's remote mode (`Platform.from_service`) pulls from.

See the repo README's "Control plane" section, and `docs/SPEC.md` (4b) for the design.

```bash
pip install -e sdk -e "server[dev]"      # from the repo root
bastion-server create-user --email you@example.com
bastion-server serve --port 8200     # http://localhost:8200/ui
pytest -q server                          # add TEST_POSTGRES_URL=... to also run on Postgres
```
