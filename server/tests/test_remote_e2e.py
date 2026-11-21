"""Item 7 against the real service: the SDK's remote mode pulls from this server."""

from pathlib import Path

import httpx
from bastion import Platform
from fastapi.testclient import TestClient

from .test_import_export import IRONCLAD, import_yaml


class InProcess(httpx.BaseTransport):
    """Send the SDK's HTTP calls to the real app in-process."""

    def __init__(self, app):
        self._client = TestClient(app)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        r = self._client.request(request.method, request.url.path, headers=dict(request.headers))
        return httpx.Response(r.status_code, headers=r.headers, content=r.content)


async def test_sdk_follows_edits_made_through_the_service(ana, tmp_path):
    client, app = ana
    import_yaml(client, IRONCLAD.read_text())
    raw = client.post("/v1/apps/ironclad/keys", json={"name": "agent-service"}).json()["key"]
    service = InProcess(app)

    p = Platform.from_service("http://testserver", raw, cache_dir=tmp_path, refresh_interval=0,
                              service_transport=service)
    assert p.app == "ironclad"
    assert p.describe_agent("banking_agent")["prompt_version"] == "v1"
    first = p.bundle_revision

    client.patch("/v1/apps/ironclad/agents/banking_agent", json={"instructions": "Be brief."})
    assert await p.refresh() is True
    assert p.bundle_revision == first + 1
    assert p.describe_agent("banking_agent")["prompt_version"] == "v2"
    assert p.config.platform.agents["ironclad.banking_agent"].instructions == "Be brief."
    assert await p.refresh() is False  # 304 now

    # rolling back in the service rolls back the running SDK too
    client.post("/v1/apps/ironclad/agents/banking_agent/rollback", json={"version": 1})
    await p.refresh()
    assert p.describe_agent("banking_agent")["prompt_version"] == "v1"
    await p.close()
