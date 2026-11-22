"""Agent-to-agent trace links: a caller's trace links to the child trace in
the other app, and the child links back to its parent."""

import uuid

from .conftest import post_traces, trace_record


def setup(client, app):
    hub = {"name": "assistant", "instructions": "Hub.", "backend": "openai", "model": "gpt-4o-mini", "tools": []}
    food = {"name": "ordering_agent", "instructions": "Food.", "backend": "openai", "model": "gpt-4o-mini", "tools": []}
    for name, agent in (("chathub", hub), ("fooddash", food)):
        assert client.post("/v1/apps", json={"name": name}).status_code == 201
        assert client.post(f"/v1/apps/{name}/agents", json=agent).status_code == 201
    return {name: client.post(f"/v1/apps/{name}/keys", json={"name": "svc"}).json()["key"]
            for name in ("chathub", "fooddash")}


def record(app, agent, **kw):
    return trace_record(app=app, agent=f"{app}.{agent}", tool_calls=[], steps=[],
                        agents=[{"agent": f"{app}.{agent}", "prompt_version": "v1", "model": "gpt-4o-mini",
                                 "status": "success", "input_tokens": 1, "output_tokens": 1, "latency_ms": 1}], **kw)


def test_parent_and_child_traces_link_both_ways(ana):
    client, app = ana
    keys = setup(client, app)
    parent_id, child_id = str(uuid.uuid4()), str(uuid.uuid4())
    parent = record("chathub", "assistant", trace_id=parent_id, chain=["chathub.assistant"],
                    child_traces=[{"app": "fooddash", "trace_id": child_id}])
    child = record("fooddash", "ordering_agent", trace_id=child_id, caller_app="chathub",
                   parent_trace_id=parent_id, chain=["chathub.assistant", "fooddash.ordering_agent"])
    assert post_traces(app, keys["chathub"], [parent]).json()["accepted"] == 1
    assert post_traces(app, keys["fooddash"], [child]).json()["accepted"] == 1

    page = client.get(f"/ui/apps/chathub/traces/{parent_id}").text
    assert f'href="/ui/apps/fooddash/traces/{child_id}"' in page
    page = client.get(f"/ui/apps/fooddash/traces/{child_id}").text
    assert f'href="/ui/apps/chathub/traces/{parent_id}"' in page
    assert "chathub.assistant → fooddash.ordering_agent" in page and "(from its key)" in page


def test_child_found_by_parent_id_even_if_the_parent_didnt_list_it(ana):
    client, app = ana
    keys = setup(client, app)
    parent_id, child_id = str(uuid.uuid4()), str(uuid.uuid4())
    post_traces(app, keys["chathub"], [record("chathub", "assistant", trace_id=parent_id)])
    post_traces(app, keys["fooddash"], [record("fooddash", "ordering_agent", trace_id=child_id,
                                               caller_app="chathub", parent_trace_id=parent_id)])
    assert f'href="/ui/apps/fooddash/traces/{child_id}"' in client.get(f"/ui/apps/chathub/traces/{parent_id}").text


def test_links_never_reach_another_owners_traces(ana, make_client):
    from .conftest import as_other_user

    client, app = ana
    keys = setup(client, app)
    parent_id = str(uuid.uuid4())
    post_traces(app, keys["chathub"], [record("chathub", "assistant", trace_id=parent_id)])
    other = as_other_user(make_client)
    assert other.post("/v1/apps", json={"name": "spy"}).status_code == 201
    other.post("/v1/apps/spy/agents", json={"name": "a", "instructions": "x", "backend": "openai",
                                            "model": "gpt-4o-mini", "tools": []})
    spy_key = other.post("/v1/apps/spy/keys", json={"name": "svc"}).json()["key"]
    child_id = str(uuid.uuid4())
    post_traces(app, spy_key, [record("spy", "a", trace_id=child_id, parent_trace_id=parent_id)])
    assert child_id not in client.get(f"/ui/apps/chathub/traces/{parent_id}").text
