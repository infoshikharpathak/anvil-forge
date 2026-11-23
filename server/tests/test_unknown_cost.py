"""Traces whose models have no price: cost is unknown (null), never $0, and
totals leave them out and say so."""

from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from alembic import command

from garrison_hq.db import alembic_config, make_engine, migrate

from .conftest import make_ironclad, post_traces, trace_record

NOW = datetime.now(timezone.utc)


def _seed(ana):
    client, app = ana
    make_ironclad(client)
    key = client.post("/v1/apps/ironclad/keys", json={}).json()["key"]
    priced = trace_record(cost_usd=0.002, started=NOW - timedelta(minutes=3))
    unknown = trace_record(model="mystery-model", cost_usd=None, unpriced_models=["mystery-model"],
                           started=NOW - timedelta(minutes=2))
    partial = trace_record(cost_usd=0.001, unpriced_models=["mystery-model"], started=NOW - timedelta(minutes=1))
    resp = post_traces(app, key, [priced, unknown, partial])
    assert resp.json()["accepted"] == 3, resp.text
    return client, priced, unknown, partial


def test_ingest_keeps_null_cost_and_flags_unpriced(ana):
    client, app = ana
    _, priced, unknown, partial = _seed(ana)
    from garrison_hq.models import Trace

    with app.state.sessionmaker() as db:
        rows = {t.trace_id: t for t in db.scalars(sa.select(Trace))}
    assert (rows[priced["trace_id"]].cost_usd, rows[priced["trace_id"]].unpriced) == (0.002, False)
    assert (rows[unknown["trace_id"]].cost_usd, rows[unknown["trace_id"]].unpriced) == (None, True)
    assert (rows[partial["trace_id"]].cost_usd, rows[partial["trace_id"]].unpriced) == (0.001, True)
    assert rows[unknown["trace_id"]].summary["unpriced_models"] == ["mystery-model"]


def test_old_sdk_records_without_the_field_still_ingest(ana):
    client, app = ana
    make_ironclad(client)
    key = client.post("/v1/apps/ironclad/keys", json={}).json()["key"]
    rec = trace_record()
    rec.pop("cost_usd")
    assert post_traces(app, key, [rec]).json()["accepted"] == 1


def test_cross_app_overview_leaves_unknown_out_of_totals(ana):
    client, *_ = _seed(ana)
    r = client.get("/ui/dashboard")
    ctx = r.context
    assert ctx["totals"]["cost_usd"] == 0.003 and ctx["totals"]["unpriced"] == 2
    (app,) = [a for a in ctx["apps"] if a["name"] == "ironclad"]
    assert (app["cost_usd"], app["unpriced"]) == (0.003, 2)
    models = {m["model"]: m for m in ctx["by_model"]}
    assert (models["mystery-model"]["cost_usd"], models["mystery-model"]["unpriced"]) == (None, 1)
    assert (models["gpt-4o-mini"]["cost_usd"], models["gpt-4o-mini"]["unpriced"]) == (0.003, 1)
    html = r.text
    assert "excludes 2 traces with no price" in html
    assert "unknown" in html and "(excl. 1 unpriced)" in html
    assert "$0.0000" not in html


def test_app_overview_says_how_many_traces_had_no_price(ana):
    client, *_ = _seed(ana)
    r = client.get("/ui/apps/ironclad/dashboard")
    week = r.context["week"]
    assert (week["cost_usd"], week["unpriced"]) == (0.003, 2)
    assert "$0.0030" in r.text and "2 traces had no price for the model" in r.text


def test_all_unknown_total_is_unknown_not_zero(ana):
    client, app = ana
    make_ironclad(client)
    key = client.post("/v1/apps/ironclad/keys", json={}).json()["key"]
    post_traces(app, key, [trace_record(model="mystery-model", cost_usd=None, unpriced_models=["mystery-model"])])
    week = client.get("/ui/apps/ironclad/dashboard").context["week"]
    assert (week["cost_usd"], week["unpriced"]) == (None, 1)
    assert client.get("/ui/dashboard").context["totals"]["cost_usd"] is None


def test_no_traces_costs_zero(ana):
    client, _ = ana
    make_ironclad(client)
    week = client.get("/ui/apps/ironclad/dashboard").context["week"]
    assert (week["cost_usd"], week["unpriced"]) == (0.0, 0)


def test_trace_detail_shows_unknown_and_partial(ana):
    client, priced, unknown, partial = _seed(ana)
    page = lambda rec: client.get(f"/ui/apps/ironclad/traces/{rec['trace_id']}").text  # noqa: E731
    assert "est. unknown" in page(unknown) and "no price for mystery-model" in page(unknown)
    assert "est. $0.00100" in page(partial) and "(partial: no price for mystery-model)" in page(partial)
    assert "est. $0.00200" in page(priced) and "no price" not in page(priced)


def test_migration_makes_cost_nullable(db_url):
    """0002 -> 0003: cost_usd goes from NOT NULL to nullable, unpriced is added."""
    command.upgrade(alembic_config(db_url), "0002")
    engine = make_engine(db_url)
    before = {c["name"]: c for c in sa.inspect(engine).get_columns("traces")}
    assert before["cost_usd"]["nullable"] is False and "unpriced" not in before
    migrate(db_url)
    after = {c["name"]: c for c in sa.inspect(sa.create_engine(db_url)).get_columns("traces")}
    assert after["cost_usd"]["nullable"] is True and after["unpriced"]["nullable"] is False
    engine.dispose()
