"""4c item 4: the cross-app dashboard (/ui/dashboard)."""

from datetime import datetime, timedelta, timezone

from .conftest import as_other_user, make_ironclad, post_traces, trace_record

NOW = datetime.now(timezone.utc)


def keyed_app(client, name):
    assert client.post("/v1/apps", json={"name": name}).status_code == 201
    assert client.post(f"/v1/apps/{name}/agents", json={"name": "helper", "instructions": "Help."}).status_code == 201
    return client.post(f"/v1/apps/{name}/keys", json={}).json()["key"]


def test_tokenomics_across_apps(ana):
    client, app = ana
    make_ironclad(client)
    ironclad_key = client.post("/v1/apps/ironclad/keys", json={}).json()["key"]
    fooddash_key = keyed_app(client, "fooddash")
    keyed_app(client, "quiet")  # no traces: still listed
    post_traces(app, ironclad_key, [
        trace_record(token_input=100, token_output=50, model="gpt-4o-mini", started=NOW - timedelta(minutes=5)),
        trace_record(token_input=10, token_output=5, model="gpt-4o-mini", status="blocked",
                     started=NOW - timedelta(days=3)),
        trace_record(token_input=1, token_output=1, started=NOW - timedelta(days=45)),  # outside 30 days
    ])
    post_traces(app, fooddash_key, [
        trace_record(app="fooddash", agent="fooddash.helper", token_input=1000, token_output=200,
                     model="claude-test", cost_usd=0.01, started=NOW - timedelta(minutes=1)),
    ])

    r = client.get("/ui/dashboard")
    assert r.status_code == 200 and r.template.name == "overview.html"
    ctx = r.context
    assert ctx["days"] == 30
    assert ctx["totals"]["total"] == 3 and ctx["totals"]["tokens"] == 150 + 15 + 1200
    apps = {a["name"]: a for a in ctx["apps"]}
    assert set(apps) == {"ironclad", "fooddash", "quiet"}
    assert (apps["ironclad"]["traces"], apps["ironclad"]["blocked"], apps["ironclad"]["tokens"]) == (2, 1, 165)
    assert apps["fooddash"]["tokens"] == 1200 and apps["quiet"]["traces"] == 0
    models = {m["model"]: m for m in ctx["by_model"]}
    assert models["claude-test"]["tokens"] == 1200 and models["gpt-4o-mini"]["tokens"] == 165
    assert ctx["by_model"][0]["model"] == "claude-test"  # most tokens first
    today = [d for d in ctx["by_day"] if d["day"] == NOW.date()][0]
    assert today["apps"] == {"ironclad": 150, "fooddash": 1200}
    assert len(ctx["bars"]["bars"]) == 30 and ctx["bars"]["peak"] == 1350
    assert [t["app"] for t in ctx["recent"]][:2] == ["fooddash", "ironclad"]
    html = r.text
    assert "/ui/apps/fooddash/dashboard" in html and "<rect" in html and "claude-test" in html

    wide = client.get("/ui/dashboard", params={"days": 90}).context
    assert wide["totals"]["total"] == 4
    assert client.get("/ui/dashboard", params={"days": "x"}).context["days"] == 30


def test_shows_only_the_users_apps(ana, make_client):
    client, app = ana
    make_ironclad(client)
    raw = client.post("/v1/apps/ironclad/keys", json={}).json()["key"]
    post_traces(app, raw, [trace_record(token_input=500)])

    bob = as_other_user(make_client)
    bob_key = keyed_app(bob, "bobsapp")
    post_traces(app, bob_key, [trace_record(app="bobsapp", agent="bobsapp.helper", token_input=7, token_output=0)])

    ctx = bob.get("/ui/dashboard").context
    assert [a["name"] for a in ctx["apps"]] == ["bobsapp"]
    assert ctx["totals"]["tokens"] == 7 and [t["app"] for t in ctx["recent"]] == ["bobsapp"]
    html = bob.get("/ui/dashboard").text
    assert "ironclad" not in html

    mine = client.get("/ui/dashboard").context
    assert [a["name"] for a in mine["apps"]] == ["ironclad"] and mine["totals"]["tokens"] == 530


def test_empty_overview(ana):
    client, _ = ana
    r = client.get("/ui/dashboard")
    assert r.status_code == 200 and "No apps yet" in r.text and "No traces yet" in r.text


def test_nav_links_to_the_dashboard(ana):
    client, _ = ana
    assert 'href="/ui/dashboard"' in client.get("/ui").text


def test_signed_out_redirects(make_client):
    client, _ = make_client()
    r = client.get("/ui/dashboard", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ui/login?next=/ui/dashboard"


def test_times_are_utc_whatever_the_database_timezone():
    """Postgres hands back timestamptz in its own TimeZone setting (e.g. IST)."""
    from datetime import timedelta as td

    from garrison_hq.routes.ui import _dt
    from garrison_hq.security import aware

    ist = timezone(td(hours=5, minutes=30))
    at = datetime(2026, 11, 22, 1, 30, tzinfo=ist)  # 20:00 UTC the day before
    assert aware(at) == at and aware(at).tzinfo == timezone.utc and aware(at).date().day == 21
    assert _dt(at) == "2026-11-21 20:00:00"
    assert aware(datetime(2026, 1, 1, 12)).tzinfo == timezone.utc  # SQLite's naive values
