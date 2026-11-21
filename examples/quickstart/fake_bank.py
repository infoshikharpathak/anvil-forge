"""A tiny stand-in bank API for the quickstart (stdlib only).

    python fake_bank.py            # serves http://localhost:8765
    python fake_bank.py 9000       # or another port
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ACCOUNTS = [
    {"id": "CHK-1001", "type": "checking", "balance": 1250.00, "currency": "USD"},
    {"id": "SAV-2002", "type": "savings", "balance": 8400.00, "currency": "USD"},
]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        if url.path != "/accounts":
            return self._send(404, {"error": "not found"})
        user = parse_qs(url.query).get("user_id", ["demo"])[0]
        self._send(200, {"user_id": user, "accounts": ACCOUNTS})

    def _send(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # keep the quickstart output clean
        pass


def serve(port: int = 8765) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    print(f"fake bank on http://localhost:{port}/accounts")
    serve(port).serve_forever()
