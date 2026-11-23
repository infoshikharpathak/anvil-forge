"""4c item 1: POST /v1/traces."""

import uuid

import pytest
from sqlalchemy import select

from garrison_hq.models import Trace

from .conftest import make_ironclad, post_traces, trace_record


@pytest.fixture
def keyed(ana):
    client, app = ana
    make_ironclad(client)
    raw = client.post("/v1/apps/ironclad/keys", json={"name": "svc"}).json()["key"]
    return client, app, raw


def stored(app):
    with app.state.sessionmaker() as db:
        return db.scalars(select(Trace).order_by(Trace.id)).all()


def test_valid_batch_is_stored(keyed):
    _, app, raw = keyed
    recs = [trace_record(), trace_record(session_id="chat-1", content={"input": "hi", "output": "hello"})]
    r = post_traces(app, raw, recs)
    assert r.status_code == 202, r.text
    assert r.json() == {"accepted": 2, "duplicates": 0, "rejected": []}
    rows = stored(app)
    assert [t.trace_id for t in rows] == [recs[0]["trace_id"], recs[1]["trace_id"]]
    t = rows[1]
    assert t.agent_name == "banking_agent" and t.agent_id is not None and t.session_id == "chat-1"
    assert (t.status, t.token_input, t.token_output, t.model) == ("success", 120, 30, "gpt-4o-mini")
    assert t.bundle_revision == 1 and t.duration_ms == 1500.0
    assert t.tool_calls[0]["tool"] == "get_accounts" and t.steps[0]["type"] == "agent_start"
    assert t.summary["agents"][0]["latency_ms"] == 1400
    assert t.content == {"input": "hi", "output": "hello"} and rows[0].content is None


def test_bare_agent_names_and_flows(keyed):
    client, app, raw = keyed
    flow = {"name": "triage", "entry": "banking_agent", "nodes": [{"agent": "banking_agent"}], "edges": []}
    assert client.post("/v1/apps/ironclad/flows", json=flow).status_code == 201
    r = post_traces(app, raw, [trace_record(agent="banking_agent"),
                               trace_record(kind="flow", flow="ironclad.triage", agent="ironclad.banking_agent"),
                               trace_record(kind="flow", flow="nope")])
    body = r.json()
    assert body["accepted"] == 2 and "flow 'nope' is not registered" in body["rejected"][0]["error"]
    assert [t.flow_name for t in stored(app)] == [None, "triage"]


def test_app_mismatch_is_rejected(keyed):
    _, app, raw = keyed
    good, bad = trace_record(), trace_record(app="other")
    body = post_traces(app, raw, [good, bad]).json()
    assert body["accepted"] == 1
    assert body["rejected"] == [{"index": 1, "trace_id": bad["trace_id"],
                                 "error": "app 'other' doesn't match the key's app 'ironclad'"}]


def test_unregistered_agent_is_rejected(keyed):
    _, app, raw = keyed
    body = post_traces(app, raw, [trace_record(agent="ironclad.ghost"),
                                  trace_record(agent="other.banking_agent")]).json()
    assert body["accepted"] == 0
    assert [r["error"] for r in body["rejected"]] == [
        "agent 'ironclad.ghost' is not registered in 'ironclad'",
        "agent 'other.banking_agent' is not registered in 'ironclad'",
    ]
    assert stored(app) == []


def test_record_validation(keyed):
    _, app, raw = keyed
    body = post_traces(app, raw, [
        trace_record(trace_id="not-a-uuid"),
        trace_record(status="weird"),
        trace_record(token_input=-1),
        "not an object",
        trace_record(content={"x": "y" * 300_000}),
    ]).json()
    errors = [r["error"] for r in body["rejected"]]
    assert body["accepted"] == 0 and len(errors) == 5
    assert "trace_id must be a UUID" in errors[0] and "status" in errors[1] and "token_input" in errors[2]
    assert "content is larger" in errors[4]


def test_retried_batch_is_not_stored_twice(keyed):
    _, app, raw = keyed
    batch = [trace_record(), trace_record()]
    assert post_traces(app, raw, batch).json()["accepted"] == 2
    again = post_traces(app, raw, batch + [batch[0]]).json()
    assert again == {"accepted": 0, "duplicates": 3, "rejected": []}
    assert len(stored(app)) == 2


def test_body_must_be_a_list_within_the_batch_limit(keyed):
    _, app, raw = keyed
    assert post_traces(app, raw, {"traces": []}).status_code == 422
    r = post_traces(app, raw, [trace_record() for _ in range(101)])
    assert r.status_code == 422 and "At most 100" in r.json()["detail"]


def test_rate_limit_per_key(ana, make_client):
    client, _ = ana
    make_ironclad(client)
    _, app = make_client(trace_rate_per_minute=5)
    raw = client.post("/v1/apps/ironclad/keys", json={"name": "a"}).json()["key"]
    other = client.post("/v1/apps/ironclad/keys", json={"name": "b"}).json()["key"]
    assert post_traces(app, raw, [trace_record() for _ in range(4)]).json()["accepted"] == 4
    r = post_traces(app, raw, [trace_record(), trace_record()])  # 4 + 2 > 5: the whole batch waits
    assert r.status_code == 429 and 1 <= int(r.headers["retry-after"]) <= 60
    assert "5 traces per minute" in r.json()["detail"]
    assert post_traces(app, raw, [trace_record()]).json()["accepted"] == 1  # exactly 5
    assert post_traces(app, raw, [trace_record()]).status_code == 429
    assert post_traces(app, other, [trace_record()]).status_code == 202  # per key
    # Rejected and duplicate records don't use up the limit.
    assert post_traces(app, raw, [trace_record(agent="ghost")]).status_code == 202


def test_auth_failures(keyed):
    client, app, raw = keyed
    from fastapi.testclient import TestClient

    anon = TestClient(app)
    assert anon.post("/v1/traces", json=[trace_record()]).status_code == 401
    for bad in ("gk_" + "x" * 43, "afk_old", raw + "x", ""):
        r = anon.post("/v1/traces", json=[trace_record()], headers={"Authorization": f"Bearer {bad}"})
        assert r.status_code == 401
    # A signed-in browser session is not an app key.
    assert client.post("/v1/traces", json=[trace_record()]).status_code == 401
    key_id = client.get("/v1/apps/ironclad/keys").json()[0]["id"]
    client.delete(f"/v1/apps/ironclad/keys/{key_id}")
    assert post_traces(app, raw, [trace_record()]).status_code == 401  # revoked
    assert stored(app) == []


def test_key_of_one_app_cannot_write_another(keyed, make_client):
    client, app, raw = keyed
    assert client.post("/v1/apps", json={"name": "fooddash"}).status_code == 201
    other_raw = client.post("/v1/apps/fooddash/keys", json={}).json()["key"]
    body = post_traces(app, other_raw, [trace_record()]).json()
    assert body["accepted"] == 0 and "doesn't match the key's app 'fooddash'" in body["rejected"][0]["error"]


def test_traces_survive_agent_deletion(keyed):
    client, app, raw = keyed
    post_traces(app, raw, [trace_record()])
    assert client.delete("/v1/apps/ironclad/agents/banking_agent").status_code == 200
    (t,) = stored(app)
    assert t.agent_id is None and t.agent_name == "banking_agent"


def test_trace_id_normalized(keyed):
    _, app, raw = keyed
    tid = uuid.uuid4()
    post_traces(app, raw, [trace_record(trace_id=str(tid).upper())])
    assert stored(app)[0].trace_id == str(tid)
