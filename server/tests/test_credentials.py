"""Granted tools: the consumer's own credential, never the granting app's auth."""

from garrison.config import load_config

from .conftest import make_ironclad

AUTH = {"header": "Authorization", "secret_env": "NOTIFY_OWN_SECRET", "prefix": "Bearer "}


def bundle(client, app="ironclad"):
    return client.get(f"/v1/apps/{app}/bundle").json()["bundle"]


def setup_notify(client):
    make_ironclad(client)
    assert client.post("/v1/apps", json={"name": "notify", "base_url": "http://notify.test", "auth": AUTH}).status_code == 201
    assert client.post("/v1/apps/notify/tools", json={"name": "send_email", "path": "/email", "trust": "trusted"}).status_code == 201


def test_grant_from_an_app_with_auth_needs_a_consumer_credential(ana):
    client, _ = ana
    setup_notify(client)
    r = client.post("/v1/apps/ironclad/grants", json={"tool": "notify.send_email"})
    assert r.status_code == 422 and "no credential for app 'notify'" in r.json()["detail"]


def test_bundle_has_the_consumer_credential_and_only_the_granting_header_scheme(ana):
    client, _ = ana
    setup_notify(client)
    assert client.patch("/v1/apps/ironclad", json={"credentials": {"notify": "env:IRONCLAD_NOTIFY_KEY"}}).status_code == 200
    assert client.post("/v1/apps/ironclad/grants", json={"tool": "notify.send_email"}).status_code == 201
    b = bundle(client)
    assert b["apps"]["ironclad"]["credentials"] == {"notify": "env:IRONCLAD_NOTIFY_KEY"}
    assert b["apps"]["notify"]["auth"] == {"header": "Authorization", "prefix": "Bearer "}
    assert "NOTIFY_OWN_SECRET" not in str(b)
    compiled = load_config(b)
    assert compiled.anvil_for("ironclad").apps["notify"].auth.secret_env == "IRONCLAD_NOTIFY_KEY"
    # the granting app's own bundle keeps its own auth
    assert bundle(client, "notify")["apps"]["notify"]["auth"]["secret_env"] == "NOTIFY_OWN_SECRET"


def test_credentials_must_be_env_references(ana):
    client, _ = ana
    make_ironclad(client)
    for bad in ({"notify": "sk-live-abc"}, {"notify": "env:lower"}, {"bad__name!": "env:X"}):
        assert client.patch("/v1/apps/ironclad", json={"credentials": bad}).status_code == 422
