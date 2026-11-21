"""A stubbed upstream for Anvil's real HTTP path (httpx MockTransport)."""

import json

import httpx


class Upstream:
    def __init__(self):
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/accounts":
            return httpx.Response(200, json={"accounts": [{"id": "CHK-1", "balance": 1200}]})
        if path == "/transfers":
            body = json.loads(request.content or b"{}")
            return httpx.Response(200, json={"transfer_id": "T-9", "status": "pending", "amount": body.get("amount")})
        if path == "/email":
            return httpx.Response(200, json={"sent": True})
        return httpx.Response(200, json={"ok": True, "path": path})

    def paths(self):
        return [r.url.path for r in self.requests]

    def transport(self):
        return httpx.MockTransport(self)
