"""Item 5: GET /v1/sdk/bundle with an app key; app keys reach nothing else."""

import re

import pytest
from anvil_forge import load_config

from .conftest import CSRF, make_ironclad


@pytest.fixture
def keyed(ana):
    client, app = ana
    make_ironclad(client)
    raw = client.post("/v1/apps/ironclad/keys", json={"name": "svc"}).json()["key"]
    return client, app, raw


def sdk_get(app, raw, **headers):
    from fastapi.testclient import TestClient

    c = TestClient(app)  # no cookies: a separate process holding only the key
    return c.get("/v1/sdk/bundle", headers={"Authorization": f"Bearer {raw}", **headers})


def test_bundle_with_revision_and_etag(keyed):
    client, app, raw = keyed
    r = sdk_get(app, raw)
    assert r.status_code == 200
    body = r.json()
    assert body["app"] == "ironclad" and body["revision"] == client.get("/v1/apps/ironclad").json()["revision"]
    assert r.headers["etag"] == f'"{body["etag"]}"'
    assert body["bundle"] == client.get("/v1/apps/ironclad/bundle").json()["bundle"]
    load_config(body["bundle"])  # exactly the SDK's config format


def test_if_none_match_returns_304_until_something_changes(keyed):
    client, app, raw = keyed
    etag = sdk_get(app, raw).headers["etag"]
    for header in (etag, etag.strip('"'), f"W/{etag}", f'"nope", {etag}'):
        not_modified = sdk_get(app, raw, **{"If-None-Match": header})
        assert not_modified.status_code == 304 and not_modified.content == b""
    client.patch("/v1/apps/ironclad/agents/banking_agent", json={"instructions": "Be brief."})
    changed = sdk_get(app, raw, **{"If-None-Match": etag})
    assert changed.status_code == 200 and changed.headers["etag"] != etag
    assert changed.json()["bundle"]["agents"]["ironclad.banking_agent"]["prompt_version"] == "v2"


def test_missing_bad_or_revoked_keys_get_401(keyed):
    client, app, raw = keyed
    from fastapi.testclient import TestClient

    assert TestClient(app).get("/v1/sdk/bundle").status_code == 401
    assert sdk_get(app, "afk_notarealkey").status_code == 401
    key_id = client.get("/v1/apps/ironclad/keys").json()[0]["id"]
    client.delete(f"/v1/apps/ironclad/keys/{key_id}")
    assert sdk_get(app, raw).status_code == 401


def test_a_session_cookie_is_not_an_app_key(keyed):
    client, _, _ = keyed
    assert client.get("/v1/sdk/bundle").status_code == 401  # signed-in browser, no key


def _fill(path: str) -> str:
    values = {"app_name": "ironclad", "tool_name": "get_accounts", "agent_name": "banking_agent",
              "flow_name": "f", "grant_id": "1", "key_id": "1"}
    return re.sub(r"\{(\w+)\}", lambda m: values.get(m.group(1), "x"), path)


def test_app_key_reaches_no_management_route(keyed):
    """Every /v1 route except the SDK API and the public auth endpoints."""
    _, app, raw = keyed
    from fastapi.testclient import TestClient

    c = TestClient(app)
    headers = {"Authorization": f"Bearer {raw}", **CSRF}
    checked = 0
    # The OpenAPI schema is the public list of every route and method.
    for path, ops in app.openapi()["paths"].items():
        if not path.startswith("/v1/") or path.startswith("/v1/sdk"):
            continue
        if path in ("/v1/auth/login", "/v1/auth/signup", "/v1/auth/logout"):
            continue
        for method in ops:
            r = c.request(method.upper(), _fill(path), headers=headers, json={})
            assert r.status_code in (401, 403), f"{method.upper()} {path} -> {r.status_code}"
            checked += 1
    assert checked >= 30  # the whole management surface was exercised
