"""4c item 3: the app's trace views (dashboard, list, detail, session)."""

from datetime import datetime, timedelta, timezone

import pytest

from .conftest import as_other_user, post_traces, trace_record
from .test_import_export import IRONCLAD, import_yaml

NOW = datetime.now(timezone.utc)


@pytest.fixture
def seeded(ana):
    client, app = ana
    import_yaml(client, IRONCLAD.read_text())
    raw = client.post("/v1/apps/ironclad/keys", json={"name": "svc"}).json()["key"]
    recs = {
        "ok": trace_record(session_id="chat-1", started=NOW - timedelta(minutes=30), bundle_revision=1),
        "blocked": trace_record(
            status="blocked", session_id="chat-1", started=NOW - timedelta(minutes=20), hook_denials=1,
            violations=[{"agent": "ironclad.banking_agent", "kind": "agent_input", "tool": None,
                         "target_agent": None, "violation": "injection_detected_agent_input"}],
            scans=[{"source": "anvil", "agent": "ironclad.banking_agent", "detected": True,
                    "violation": "injection_detected_agent_input", "tool": None, "pattern": "ignore previous"}],
            tool_calls=[], token_input=10, token_output=0),
        "error": trace_record(status="error", started=NOW - timedelta(days=2), duration_ms=800.0),
        "content": trace_record(
            session_id="chat-1", started=NOW - timedelta(minutes=10),
            content={"input": "What are my accounts?", "output": "You have CHK-1.", "error": None,
                     "agents": [{"agent": "ironclad.banking_agent", "prompt": "You are the bank bot.",
                                 "input": "What are my accounts?", "raw_output": "You have CHK-1.",
                                 "output": "You have CHK-1."}],
                     "tool_calls": [{"tool": "get_accounts", "params": {"user_id": "demo_user"},
                                     "response": {"accounts": []}, "correction": None}]},
            tool_calls=[{"tool": "notify__send_email", "tool_id": "notify.send_email", "app": "notify",
                         "agent": "ironclad.banking_agent", "duration_ms": 12.0, "status": "blocked",
                         "violation": "cross_app_denied", "redacted_fields": []},
                        {"tool": "initiate_transfer", "tool_id": "ironclad.initiate_transfer", "app": "ironclad",
                         "agent": "ironclad.banking_agent", "duration_ms": 30.0, "status": "allowed",
                         "violation": None, "redacted_fields": ["otp_code"]}]),
    }
    assert post_traces(app, raw, list(recs.values())).json()["accepted"] == 4
    return client, app, raw, recs


def get(client, path, template):
    r = client.get(path)
    assert r.status_code == 200, r.text[:500]
    assert r.template.name == template
    return r


def test_signed_out_trace_pages_redirect(make_client):
    client, _ = make_client()
    for path in ("/ui/apps/ironclad/dashboard", "/ui/apps/ironclad/traces", "/ui/apps/ironclad/traces/x",
                 "/ui/apps/ironclad/sessions/s"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == f"/ui/login?next={path}"


def test_app_dashboard(seeded):
    client, _, _, recs = seeded
    r = get(client, "/ui/apps/ironclad/dashboard", "dashboard.html")
    ctx = r.context
    assert ctx["today"]["total"] >= 3  # the "error" one is 2 days old
    assert ctx["week"] == {**ctx["week"], "total": 4, "success": 2, "blocked": 1, "error": 1}
    assert ctx["chart"]["sum"] == 4 and ctx["chart"]["total"].count(",") == 7 * 24
    (agent,) = ctx["agents"]
    assert agent["agent"] == "banking_agent" and agent["count"] == 4 and agent["avg_ms"] == pytest.approx(1325.0)
    assert [b["trace_id"] for b in ctx["blocked"]] == [recs["blocked"]["trace_id"]]
    html = r.text
    assert "<svg" in html and "<polyline" in html and "<script" not in html.split("</main>")[0].split("<main>")[1]
    assert recs["blocked"]["trace_id"] in html and 'class="badge blocked"' in html


def test_empty_dashboard_explains_how_to_connect(ana):
    client, _ = ana
    import_yaml(client, IRONCLAD.read_text())
    html = get(client, "/ui/apps/ironclad/dashboard", "dashboard.html").text
    assert "No traces yet" in html and "BASTION_KEY" in html


def test_traces_list_filters(seeded):
    client, _, _, recs = seeded
    rows = get(client, "/ui/apps/ironclad/traces", "traces.html").context["rows"]
    assert [r["trace_id"] for r in rows] == [recs[k]["trace_id"] for k in ("content", "blocked", "ok", "error")]

    def ids(**q):
        r = client.get("/ui/apps/ironclad/traces", params=q)
        assert r.status_code == 200
        return {row["trace_id"] for row in r.context["rows"]}

    assert ids(status="blocked") == {recs["blocked"]["trace_id"]}
    assert ids(session="chat-1") == {recs[k]["trace_id"] for k in ("ok", "blocked", "content")}
    assert ids(agent="banking_agent") == {r["trace_id"] for r in recs.values()}
    assert ids(agent="nobody") == set()
    day = (NOW - timedelta(days=2)).date().isoformat()
    assert ids(**{"from": day, "to": day}) == {recs["error"]["trace_id"]}
    assert ids(**{"from": NOW.date().isoformat()}) >= {recs["content"]["trace_id"]}
    bad = client.get("/ui/apps/ironclad/traces", params={"from": "yesterday", "status": "odd"})
    assert "must be a date" in bad.text and "Unknown status" in bad.text
    html = client.get("/ui/apps/ironclad/traces").text
    assert "chat-1" in html and 'class="badge error"' in html and "1.50 s" in html


def test_traces_list_paginates_50_per_page(ana):
    client, app = ana
    import_yaml(client, IRONCLAD.read_text())
    raw = client.post("/v1/apps/ironclad/keys", json={}).json()["key"]
    recs = [trace_record(started=NOW - timedelta(seconds=i)) for i in range(60)]
    assert post_traces(app, raw, recs[:50]).json()["accepted"] == 50
    assert post_traces(app, raw, recs[50:]).json()["accepted"] == 10
    first = get(client, "/ui/apps/ironclad/traces", "traces.html").context
    assert (first["total"], first["pages"], len(first["rows"])) == (60, 2, 50)
    assert first["rows"][0]["trace_id"] == recs[0]["trace_id"]  # newest first
    assert first["next_url"] == "?page=2" and first["prev_url"] is None
    second = client.get("/ui/apps/ironclad/traces", params={"page": 2}).context
    assert len(second["rows"]) == 10 and second["rows"][-1]["trace_id"] == recs[-1]["trace_id"]
    filtered = client.get("/ui/apps/ironclad/traces", params={"status": "success", "page": 9}).context
    assert filtered["page"] == 2 and filtered["prev_url"] == "?status=success&page=1"


def test_trace_detail(seeded):
    client, _, _, recs = seeded
    rec = recs["content"]
    r = get(client, f"/ui/apps/ironclad/traces/{rec['trace_id']}", "trace.html")
    t = r.context["t"]
    assert (t["trace_id"], t["agent"], t["session_id"], t["model"]) == (
        rec["trace_id"], "banking_agent", "chat-1", "gpt-4o-mini")
    assert t["prompt"] == {"label": "v1", "exists": True, "href": "/ui/apps/ironclad#v-banking_agent-v1"}
    assert [s["type"] for s in t["timeline"]["steps"]] == ["agent_start", "tool_call", "agent_end"]
    assert [c["cross_app"] for c in t["tool_calls"]] == [True, False]
    html = r.text
    assert "cross-app" in html and "cross_app_denied" in html and "otp_code" in html
    assert "Content" in html and "You are the bank bot." in html and "demo_user" in html
    assert 'class="bar"' in html


def test_trace_detail_without_content_and_with_a_scan(seeded):
    client, _, _, recs = seeded
    html = get(client, f"/ui/apps/ironclad/traces/{recs['blocked']['trace_id']}", "trace.html").text
    assert "<h2>Content</h2>" not in html
    assert "injection_detected_agent_input" in html and "ignore previous" in html
    assert "Hook denials" in html


def test_prompt_link_follows_the_bundle_revision(seeded):
    client, app, raw, _ = seeded
    client.patch("/v1/apps/ironclad/agents/banking_agent", json={"instructions": "Be brief."})  # v2, new revision
    rev = client.get("/v1/apps/ironclad").json()["revision"]
    rec = trace_record(bundle_revision=rev, prompt_version="stale-label")
    post_traces(app, raw, [rec])
    t = client.get(f"/ui/apps/ironclad/traces/{rec['trace_id']}").context["t"]
    assert t["prompt"]["label"] == "v2" and t["prompt"]["href"].endswith("#v-banking_agent-v2")
    assert 'id="v-banking_agent-v2"' in client.get("/ui/apps/ironclad").text


def test_one_trace_id_several_runs(seeded):
    client, app, raw, _ = seeded
    a = trace_record(started=NOW - timedelta(seconds=5))
    b = trace_record(trace_id=a["trace_id"], started=NOW)
    post_traces(app, raw, [a, b])
    r = client.get(f"/ui/apps/ironclad/traces/{a['trace_id']}")
    assert len(r.context["runs"]) == 2 and r.context["t"]["run_id"] == a["run_id"]
    assert client.get(f"/ui/apps/ironclad/traces/{a['trace_id']}", params={"run": b["run_id"]}).context["t"]["run_id"] == b["run_id"]


def test_session_view(seeded):
    client, _, _, recs = seeded
    r = get(client, "/ui/apps/ironclad/sessions/chat-1", "session.html")
    assert [t["trace_id"] for t in r.context["turns"]] == [recs[k]["trace_id"] for k in ("ok", "blocked", "content")]
    html = r.text
    assert "Turn 3" in html and "What are my accounts?" in html and "Metadata only" in html


def test_missing_trace_and_session_are_404(seeded):
    client, *_ = seeded
    assert client.get("/ui/apps/ironclad/traces/00000000-0000-0000-0000-000000000000").status_code == 404
    assert client.get("/ui/apps/ironclad/sessions/nope").status_code == 404


def test_other_users_trace_pages_are_404(seeded, make_client):
    _, _, _, recs = seeded
    bob = as_other_user(make_client)
    for path in ("/ui/apps/ironclad/dashboard", "/ui/apps/ironclad/traces",
                 f"/ui/apps/ironclad/traces/{recs['ok']['trace_id']}", "/ui/apps/ironclad/sessions/chat-1"):
        assert bob.get(path).status_code == 404, path


def test_apps_list_links_to_the_dashboard(seeded):
    client, *_ = seeded
    html = client.get("/ui").text
    assert 'href="/ui/apps/ironclad/dashboard"' in html
    assert 'href="/ui/apps/ironclad/traces"' in client.get("/ui/apps/ironclad").text  # tabs on config too


def test_timeline_span_is_the_run_not_offset_plus_duration(seeded):
    client, _, _, recs = seeded
    t = client.get(f"/ui/apps/ironclad/traces/{recs['ok']['trace_id']}").context["t"]
    assert t["timeline"]["span_ms"] == 1500.0  # agent_end at 1500 ms is the end, not 1500 + its duration
    end = t["timeline"]["steps"][-1]
    assert end["type"] == "agent_end"
