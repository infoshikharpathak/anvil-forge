"""Item 8: server-rendered pages."""

import hashlib
from pathlib import Path

from .conftest import as_other_user
from .test_import_export import IRONCLAD, import_yaml


def test_signed_out_pages_redirect_to_login(make_client):
    client, _ = make_client()
    for path in ("/ui", "/ui/apps/ironclad", "/ui/audit"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == f"/ui/login?next={path}"
    assert client.get("/", follow_redirects=False).headers["location"] == "/ui"
    page = client.get("/ui/login")
    assert page.status_code == 200 and 'data-api="POST /v1/auth/login"' in page.text


def test_login_next_never_leaves_the_site(make_client):
    client, _ = make_client()
    page = client.get("/ui/login", params={"next": "https://evil.example/phish"}).text
    assert 'data-then="/ui"' in page and "evil.example" not in page


def test_signup_page_follows_the_setting(make_client):
    closed, _ = make_client()
    assert "Sign-up is closed" in closed.get("/ui/signup").text
    opened, _ = make_client(signup_open=True)
    assert 'data-api="POST /v1/auth/signup"' in opened.get("/ui/signup").text


def test_apps_and_app_detail_render(ana):
    client, _ = ana
    import_yaml(client, IRONCLAD.read_text())
    client.patch("/v1/apps/ironclad/agents/banking_agent", json={"instructions": "Be brief."})
    raw = client.post("/v1/apps/ironclad/keys", json={"name": "agent-service"}).json()["key"]

    apps = client.get("/ui").text
    assert "/ui/apps/ironclad" in apps and ">2<" not in apps.split("ironclad")[0]
    page = client.get("/ui/apps/ironclad")
    assert page.status_code == 200
    html = page.text
    assert "banking_agent" in html and "v2 active" in html
    assert 'data-body=\'{"version": 1}\'' in html  # roll back to v1
    assert "initiate_transfer" in html and "otp_code" in html  # tool list with its redactions
    assert raw[:12] in html and raw not in html  # prefix only
    assert hashlib.sha256(raw.encode()).hexdigest() not in html
    for section in ('id="agents"', 'id="tools"', 'id="flows"', 'id="grants"', 'id="keys"'):
        assert section in html


def test_audit_page_lists_changes(ana):
    client, _ = ana
    import_yaml(client, IRONCLAD.read_text())
    html = client.get("/ui/audit").text
    assert "import" in html and "ironclad" in html


def test_other_users_app_page_is_404(ana, make_client):
    client, _ = ana
    import_yaml(client, IRONCLAD.read_text())
    bob = as_other_user(make_client)
    assert bob.get("/ui/apps/ironclad").status_code == 404
    assert "/ui/apps/ironclad" not in bob.get("/ui").text  # no link to ana's app


def test_ui_writes_send_the_csrf_header():
    js = (Path(__file__).resolve().parents[1] / "bastion_server" / "static" / "app.js").read_text()
    assert '"X-Bastion": "1"' in js and 'credentials: "same-origin"' in js
    assert "window.confirm" not in js.replace("without window.confirm", "")


def test_static_assets_served(make_client):
    client, _ = make_client()
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200
